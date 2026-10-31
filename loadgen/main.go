// loadgen replays pre-rendered search requests against Postgres or Elasticsearch and
// reports the latency of each one. The requests come from the Python side (bench/run.py),
// so the SQL and Elasticsearch bodies are defined in one place.
//
// Input (JSON lines): a header {"kind": "pg"|"es", "dsn"|"url", "setup": [...]} and then
// one request per line, in schedule order: {"sql", "args"} or {"path", "params", "body"}.
// Request i goes to client i % concurrency, as in the Python runner.
//
// Closed loop (-rate 0): every client sends its next request as soon as the last returns.
// Open loop (-rate R): request i is due at start + i/R. An idle client waits for that time
// and the clock starts when it sends; a client still busy with its previous request starts
// late, and that queueing time counts.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

type header struct {
	Kind  string   `json:"kind"`
	DSN   string   `json:"dsn"`
	URL   string   `json:"url"`
	Setup []string `json:"setup"`
}

type request struct {
	SQL    string            `json:"sql"`
	Args   []string          `json:"args"`
	Path   string            `json:"path"`
	Params map[string]string `json:"params"`
	Body   json.RawMessage   `json:"body"`
}

type job struct {
	slot int
	req  request
}

type result struct {
	LatMs  []float64 `json:"lat_ms"`
	Errors []string  `json:"errors"`
	First  float64   `json:"first"` // seconds since the Unix epoch
	End    float64   `json:"end"`
}

type sender interface {
	send(ctx context.Context, r request) error
	close()
}

type pgSender struct{ conn *pgx.Conn }

func newPg(ctx context.Context, h header) (sender, error) {
	conn, err := pgx.Connect(ctx, h.DSN)
	if err != nil {
		return nil, err
	}
	for _, s := range h.Setup {
		if _, err := conn.Exec(ctx, s); err != nil {
			return nil, fmt.Errorf("%s: %w", s, err)
		}
	}
	return &pgSender{conn}, nil
}

func (p *pgSender) send(ctx context.Context, r request) error {
	args := make([]any, len(r.Args))
	for i, a := range r.Args {
		args[i] = a
	}
	rows, err := p.conn.Query(ctx, r.SQL, args...)
	if err != nil {
		return err
	}
	for rows.Next() { // read every row, as the Python client does
		if _, err := rows.Values(); err != nil {
			rows.Close()
			return err
		}
	}
	rows.Close()
	return rows.Err()
}

func (p *pgSender) close() { p.conn.Close(context.Background()) }

type esSender struct {
	base   string
	client *http.Client
}

func newEs(h header) sender {
	// One keep-alive connection per client, like one HTTP client per Python process.
	return &esSender{h.URL, &http.Client{
		Timeout:   60 * time.Second,
		Transport: &http.Transport{MaxIdleConnsPerHost: 1, DisableCompression: true},
	}}
}

func (e *esSender) send(ctx context.Context, r request) error {
	u := e.base + r.Path
	if len(r.Params) > 0 {
		q := url.Values{}
		for k, v := range r.Params {
			q.Set(k, v)
		}
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, "POST", u, bytes.NewReader(r.Body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := e.client.Do(req)
	if err != nil {
		return err
	}
	_, err = io.Copy(io.Discard, resp.Body) // read the whole response
	resp.Body.Close()
	if err == nil && resp.StatusCode != http.StatusOK {
		err = fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return err
}

func (e *esSender) close() { e.client.CloseIdleConnections() }

func epoch(t time.Time) float64 { return float64(t.UnixNano()) / 1e9 }

func main() {
	concurrency := flag.Int("concurrency", 1, "clients, one connection each")
	rate := flag.Float64("rate", 0, "open loop: requests per second (0 = closed loop)")
	startAt := flag.Float64("start-at", 0, "Unix time of the first scheduled request (0 = now + 1s)")
	flag.Parse()

	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 64<<20)
	var h header
	in.Scan()
	if err := json.Unmarshal(in.Bytes(), &h); err != nil {
		fmt.Fprintln(os.Stderr, "header:", err)
		os.Exit(2)
	}
	queues := make([][]job, *concurrency)
	for slot := 0; in.Scan(); slot++ {
		var r request
		if err := json.Unmarshal(in.Bytes(), &r); err != nil {
			fmt.Fprintln(os.Stderr, "request:", err)
			os.Exit(2)
		}
		queues[slot%*concurrency] = append(queues[slot%*concurrency], job{slot, r})
	}

	ctx := context.Background()
	senders := make([]sender, *concurrency)
	for i := range senders { // connect everyone before the clock starts
		var err error
		if h.Kind == "pg" {
			senders[i], err = newPg(ctx, h)
		} else {
			senders[i] = newEs(h)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "connect:", err)
			os.Exit(2)
		}
	}

	t0 := time.Now().Add(time.Second)
	if *startAt > 0 {
		t0 = time.Unix(0, int64(*startAt*1e9))
	}
	var interval time.Duration
	if *rate > 0 {
		interval = time.Duration(float64(time.Second) / *rate)
	}

	results := make([]result, *concurrency)
	var wg sync.WaitGroup
	for i := range senders {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			res := &results[i]
			time.Sleep(time.Until(t0))
			res.First = epoch(time.Now())
			if interval > 0 {
				res.First = epoch(t0)
			}
			for _, j := range queues[i] {
				var start time.Time
				if interval > 0 {
					due := t0.Add(time.Duration(j.slot) * interval)
					if d := time.Until(due); d > 0 {
						time.Sleep(d)
						start = time.Now()
					} else {
						start = due
					}
				} else {
					start = time.Now()
				}
				if err := senders[i].send(ctx, j.req); err != nil {
					res.Errors = append(res.Errors, fmt.Sprintf("slot %d: %v", j.slot, err))
					continue
				}
				res.LatMs = append(res.LatMs, float64(time.Since(start).Microseconds())/1000)
			}
			res.End = epoch(time.Now())
			senders[i].close()
		}(i)
	}
	wg.Wait()

	out := result{LatMs: []float64{}, Errors: []string{}, First: results[0].First, End: results[0].End}
	for _, r := range results {
		out.LatMs = append(out.LatMs, r.LatMs...)
		out.Errors = append(out.Errors, r.Errors...)
		if r.First < out.First {
			out.First = r.First
		}
		if r.End > out.End {
			out.End = r.End
		}
	}
	json.NewEncoder(os.Stdout).Encode(out)
}

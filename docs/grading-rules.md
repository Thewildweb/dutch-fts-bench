# Grading rules

Every (search, job ad) pair in the judged pool gets a grade 0, 1 or 2. The same rules apply to
the LLM grader and to the hand-graded calibration samples. Grade as a job seeker who typed the
search into a job site, on the whole ad (up to 10,000 characters).

Search syntax: all words should apply; "quoted words" are an exact phrase; A OR B means either;
-word means the searcher does not want that.

## 2: good match

The job itself is what was searched for: the role is in the title or clearly the core of the
job, and every explicit wish fits (phrase, place, hours, level).

- Word forms and plurals count ("verpleegkundigen" = "verpleegkundige").
- With A OR B, one of them is enough.
- A search without a level accepts junior, medior and senior.

## 1: partial match

- Right field, but an explicit wish does not fit: level, hours, place, or a detail such as
  "groep 8" when the ad is for "groep 5".
- An internship, traineeship or student job when the search did not ask for one.
- A related role in which the searched skill is a real but secondary part (a DevOps role with
  some React, for "react typescript").
- A clearly lower or higher tier of the same work (an administrative assistant, for
  "boekhouder").

## 0: no match

- The search words only appear in passing: the company description, a list of other
  vacancies on the same page, recruiter or benefits text, or as colleagues ("je werkt samen
  met teamleiders").
- A different job that only touches the subject ("Docent zorg en welzijn", for a nurse).
- It breaks an exclusion ("-stage" while the ad is an internship).

## Why these rules

Every engine applies the same matching rules, so nearly every result contains the search
words. The grades therefore measure ranking: whether an engine puts the ads that are about the
searched job above ads that only mention the words. Calibration round 1 (20 pairs, sampled to
contain many low grades) showed the grader was most often more lenient than a person on exactly
these cases: words in lists of other vacancies, colleagues, and different jobs in the same field.

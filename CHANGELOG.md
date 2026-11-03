# Urbris changelog

Newest first. One entry per change set to the site or server.

## 2026-11-02
- Flight calculator on /learn (/learn#calc, linked from the nav and the Private pilot track). E6B-style tools that update as you type: wind correction angle, true/magnetic heading and groundspeed; leg time and fuel with CAR 602.88 day/night VFR reserves and an optional fuel-on-board check; runway crosswind/headwind with gusts and a max-demonstrated-crosswind warning; pressure and density altitude with true airspeed; a time-speed-distance solver; and a unit converter. Last inputs are remembered on the device.
- M1 and G1 knowledge-test practice are live on /learn. Original questions from the MTO handbooks: 47 shared rules-of-the-road and signs questions, plus 27 motorcycle (M1 rules, riding skills, hazards) and 10 G1-licence questions. 40-question mock exams at an 80% pass mark, section practice with explanations. One quiz engine now runs PSTAR, M1 and G1, with separate score history for each.
- PSTAR practice now uses the Transport Canada TP 11919 study-guide questions (191 questions, 14 sections) in place of the original set. Answers follow the official TC key; 11 questions from the older edition supplied were set from the CARs where the current key doesn't match their wording. Heliport-markings question 4.10 left out (needs its diagram). Options keep their published order; question numbers shown for cross-reference.
- New /learn section: Ride (M1/M2/M), Drive (G1/G2/G manual) and Fly (PSTAR/ROC-A/PPL) training tracks.
- PSTAR practice is live: 67 original questions across 11 topics, 50-question mock exam scored at 90%, topic practice with explanations and CARs/AIM references, retry-missed, local score history.
- Added a Learn link to the homepage nav.
- Changed the accent colour from orange to green across the site (buttons, highlights, map markers, focus states). Hazard/warning amber unchanged.
- Added `.env.example` listing every environment variable the server reads.
- Added `.env` to `.gitignore`.
- Started this changelog.

## 2026-08-26 (baseline)
- Last commit before the changelog: real curviness measurement and sorting in the Hamilton road database.

# codespell derived candidate dictionary

`en-candidates.json` is a separately licensed adaptation of the codespell v2.4.3 dictionary. It is distributed under **Creative Commons Attribution-ShareAlike 3.0 Unported**. The proprietary license at the repository root does not restrict the rights granted for this data. Downstream recipients may redistribute and adapt this data under CC BY-SA 3.0.

Attribution: English Wikipedia contributors and codespell contributors. Upstream credits the dictionaries as derivatives of English Wikipedia. No codespell program code was copied into Inkwise.

- [Upstream repository and copyright statement](https://github.com/codespell-project/codespell/blob/57b21406f092110c18776e39b0bda50d37c945c8/README.rst#license)
- [Wikipedia source lineage linked by upstream](https://en.wikipedia.org/wiki/Wikipedia:Lists_of_common_misspellings/For_machines)
- [Pinned dictionary](https://raw.githubusercontent.com/codespell-project/codespell/57b21406f092110c18776e39b0bda50d37c945c8/codespell_lib/data/dictionary.txt)
- Version: v2.4.3, commit `57b21406f092110c18776e39b0bda50d37c945c8`.
- Source SHA-256: `a457564a466120c728361e9c759b6a6ef05c2acc05c7e12d1ba0eb251036f42d`.
- [License](https://creativecommons.org/licenses/by-sa/3.0/) and complete local text: [LICENSE.txt](LICENSE.txt).

Changes by Inkwise, 2026-10-10: select lowercase ASCII pairs with a single correction and 3–16 letters; deduplicate; choose 6000 pairs by stable SHA-256 ordering; serialize JSON; add provenance, candidate status and contextual-review guidance. The derivative data and its accompanying metadata carry the same CC BY-SA 3.0 license. No additional restriction is imposed on this data. The candidate status describes suitability, not a license restriction.

Selection is not based on real student frequency or age appropriateness. Technical vocabulary may remain. These are spelling candidates, not teacher-approved K12 rules or a corpus of students' work. The program rejects activation while `reviewStatus` is `candidate`.

Reproduction: download the pinned dictionary, then run `python scripts/prepare_lexicon.py --source PATH_TO_DICTIONARY`. The script checks the upstream hash and reproduces the committed JSON; it refuses to overwrite differing output.

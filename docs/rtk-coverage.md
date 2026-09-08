RTK version: rtk 0.48.0
Numbers are UTF-8 bytes and estimated tokens (bytes / 3.5).
Fixture runtime: Python 3.12 with pytest 8.x.

| command | raw bytes/tokens | rtk bytes/tokens (%) | tokenpipe bytes/tokens (%) |
| --- | ---: | ---: | ---: |
| git status | 787/225 (100.0%) | 375/108 (47.6%) | 787/225 (100.0%) |
| git status --porcelain | 368/106 (100.0%) | 367/105 (99.7%) | 366/105 (99.5%) |
| git diff | 5509/1574 (100.0%) | 4985/1425 (90.5%) | 5509/1574 (100.0%) |
| git log -n 20 | 2979/852 (100.0%) | 1040/298 (34.9%) | 2979/852 (100.0%) |
| git show HEAD | 362/104 (100.0%) | 229/66 (63.3%) | 362/104 (100.0%) |
| ls -la project | 687/197 (100.0%) | 126/36 (18.3%) | 686/196 (99.9%) |
| cat synthetic_module.py | 16090/4598 (100.0%) | 16090/4598 (100.0%) | 16090/4598 (100.0%) |
| head -50 synthetic_module.py | 2640/755 (100.0%) | 2603/744 (98.6%) | 2640/755 (100.0%) |
| tail -50 synthetic_module.py | 2700/772 (100.0%) | 2700/772 (100.0%) | 2700/772 (100.0%) |
| wc -l synthetic_module.py | 86/25 (100.0%) | 18/6 (20.9%) | 80/23 (93.0%) |
| rg synthetic_function project | 39190/11198 (100.0%) | 2317/662 (5.9%) | 2664/762 (6.8%) |
| find Python files | 217/62 (100.0%) | 46/14 (21.2%) | 216/62 (99.5%) |
| jq fixture.json | 348/100 (100.0%) | 349/100 (100.3%) | 125/36 (35.9%) |
| pytest passing | 493/141 (100.0%) | 18/6 (3.7%) | 492/141 (99.8%) |
| pytest failing | 2632/752 (100.0%) | 794/227 (30.2%) | 2632/752 (100.0%) |

Winner per row: git status: rtk; git status --porcelain: tokenpipe; git diff: rtk; git log -n 20: rtk; git show HEAD: rtk; ls -la project: rtk; cat synthetic_module.py: neither; head -50 synthetic_module.py: rtk; tail -50 synthetic_module.py: neither; wc -l synthetic_module.py: rtk; rg synthetic_function project: rtk; find Python files: rtk; jq fixture.json: tokenpipe; pytest passing: rtk; pytest failing: rtk

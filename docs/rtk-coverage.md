RTK version: rtk 0.48.0
Numbers are UTF-8 bytes and estimated tokens (bytes / 3.5).

| command | raw bytes/tokens | rtk bytes/tokens (%) | tokenpipe bytes/tokens (%) |
| --- | ---: | ---: | ---: |
| git status | 269/77 (100.0%) | 20/6 (7.4%) | 268/77 (99.6%) |
| git status --porcelain | 13/4 (100.0%) | 12/4 (92.3%) | 11/4 (84.6%) |
| git diff | 161/46 (100.0%) | 157/45 (97.5%) | 161/46 (100.0%) |
| git log -n 20 | 299/86 (100.0%) | 106/31 (35.5%) | 299/86 (100.0%) |
| git show HEAD | 298/86 (100.0%) | 187/54 (62.8%) | 298/86 (100.0%) |
| ls -la project | 472/135 (100.0%) | 97/28 (20.6%) | 471/135 (99.8%) |
| cat synthetic_module.py | 16090/4598 (100.0%) | 16090/4598 (100.0%) | 16090/4598 (100.0%) |
| head -50 synthetic_module.py | 2640/755 (100.0%) | 2603/744 (98.6%) | 2640/755 (100.0%) |
| tail -50 synthetic_module.py | 2700/772 (100.0%) | 2700/772 (100.0%) | 2700/772 (100.0%) |
| wc -l synthetic_module.py | 86/25 (100.0%) | 18/6 (20.9%) | 80/23 (93.0%) |
| rg synthetic_function project | 39190/11198 (100.0%) | 2317/662 (5.9%) | 2664/762 (6.8%) |
| find Python files | 217/62 (100.0%) | 46/14 (21.2%) | 216/62 (99.5%) |
| jq fixture.json | 348/100 (100.0%) | 349/100 (100.3%) | 125/36 (35.9%) |
| pytest passing | 0/0 (0.0%) | skipped (pytest missing) | 0/0 (0.0%) |
| pytest failing | 0/0 (0.0%) | skipped (pytest missing) | 0/0 (0.0%) |

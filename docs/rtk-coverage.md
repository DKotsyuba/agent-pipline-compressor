Measured against the locally installed RTK.
Numbers are UTF-8 bytes and estimated tokens (bytes / 3.5).
Fixtures are generated at run time; pytest rows need a Python with pytest importable.

| command | raw bytes/tokens | rtk bytes/tokens (%) | tokenpipe bytes/tokens (%) |
| --- | ---: | ---: | ---: |
| git status | 787/257 (100.0%) | 375/134 (47.6%) | 787/257 (100.0%) |
| git status --porcelain | 368/131 (100.0%) | 367/130 (99.7%) | 366/130 (99.5%) |
| git diff | 5509/1961 (100.0%) | 4985/1832 (90.5%) | 5509/1961 (100.0%) |
| git log -n 20 | 2979/1182 (100.0%) | 1040/330 (34.9%) | 2979/1182 (100.0%) |
| git show HEAD | 362/139 (100.0%) | 229/82 (63.3%) | 362/139 (100.0%) |
| ls -la project | 687/234 (100.0%) | 126/46 (18.3%) | 686/233 (99.9%) |
| cat synthetic_module.py | 16090/5251 (100.0%) | 16090/5251 (100.0%) | 16090/5251 (100.0%) |
| head -50 synthetic_module.py | 2640/859 (100.0%) | 2603/847 (98.6%) | 2640/859 (100.0%) |
| tail -50 synthetic_module.py | 2700/883 (100.0%) | 2700/883 (100.0%) | 2700/883 (100.0%) |
| wc -l synthetic_module.py | 122/50 (100.0%) | 18/8 (14.8%) | 116/48 (95.1%) |
| rg synthetic_function project | 49990/19163 (100.0%) | 2421/826 (4.8%) | 1156/444 (2.3%) |
| find Python files | 325/137 (100.0%) | 46/17 (14.2%) | 324/136 (99.7%) |
| jq fixture.json | 348/153 (100.0%) | 349/154 (100.3%) | 125/58 (35.9%) |
| pytest passing | 537/247 (100.0%) | 18/6 (3.4%) | 536/246 (99.8%) |
| pytest failing | 2676/1094 (100.0%) | 875/307 (32.7%) | 2676/1094 (100.0%) |

Winner per row: git status: rtk; git status --porcelain: tokenpipe; git diff: rtk; git log -n 20: rtk; git show HEAD: rtk; ls -la project: rtk; cat synthetic_module.py: neither; head -50 synthetic_module.py: rtk; tail -50 synthetic_module.py: neither; wc -l synthetic_module.py: rtk; rg synthetic_function project: tokenpipe; find Python files: rtk; jq fixture.json: tokenpipe; pytest passing: rtk; pytest failing: rtk

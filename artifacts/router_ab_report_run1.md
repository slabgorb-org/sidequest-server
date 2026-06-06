# Router A/B Evaluation Report — Haiku vs local qwen

- Generated: 2026-06-06T04:55:56.304421+00:00
- Sample size: 150

| Metric | Haiku | qwen |
|--------|-------|------|
| schema validity % | 100.0 | 86.0 |
| latency p50 ms | 4557 | 17156 |
| latency p95 ms | 8806 | 24134 |

**Dispatch-selection agreement:** 26.7%

## Per-dispatch-type agreement

| Dispatch type | Agreed | Total |
|---------------|--------|-------|
| audience | 0 | 1 |
| chase | 0 | 4 |
| confrontation | 17 | 67 |
| distinctive_detail_hint | 0 | 2 |
| equip | 0 | 10 |
| inventory | 0 | 1 |
| inventory_management | 0 | 1 |
| magic_working | 3 | 16 |
| movement | 15 | 49 |
| npc_agency | 1 | 24 |
| persuasion | 0 | 1 |
| reflect_absence | 0 | 1 |
| scenario_clue | 2 | 22 |
| ship_combat | 0 | 1 |
| witnessed_act | 0 | 4 |
| wry_whimsy:scenario_clue | 0 | 1 |

## Per-capture

| # | Haiku valid | qwen valid | agreement |
|---|-------------|------------|-----------|
| 0 | True | True | True |
| 1 | True | True | False |
| 2 | True | False | False |
| 3 | True | True | True |
| 4 | True | True | False |
| 5 | True | True | True |
| 6 | True | True | False |
| 7 | True | True | False |
| 8 | True | True | True |
| 9 | True | True | False |
| 10 | True | False | False |
| 11 | True | False | False |
| 12 | True | True | False |
| 13 | True | True | False |
| 14 | True | True | False |
| 15 | True | True | False |
| 16 | True | True | True |
| 17 | True | True | True |
| 18 | True | False | False |
| 19 | True | True | False |
| 20 | True | True | False |
| 21 | True | True | True |
| 22 | True | True | True |
| 23 | True | True | False |
| 24 | True | True | False |
| 25 | True | True | True |
| 26 | True | True | False |
| 27 | True | False | False |
| 28 | True | False | False |
| 29 | True | True | False |
| 30 | True | False | False |
| 31 | True | True | False |
| 32 | True | True | False |
| 33 | True | True | False |
| 34 | True | True | False |
| 35 | True | True | False |
| 36 | True | True | True |
| 37 | True | True | False |
| 38 | True | True | True |
| 39 | True | True | False |
| 40 | True | True | True |
| 41 | True | True | True |
| 42 | True | True | False |
| 43 | True | True | False |
| 44 | True | True | False |
| 45 | True | False | False |
| 46 | True | True | True |
| 47 | True | True | False |
| 48 | True | True | True |
| 49 | True | True | False |
| 50 | True | True | False |
| 51 | True | True | True |
| 52 | True | True | False |
| 53 | True | True | False |
| 54 | True | True | False |
| 55 | True | True | False |
| 56 | True | True | False |
| 57 | True | True | False |
| 58 | True | True | False |
| 59 | True | True | True |
| 60 | True | True | False |
| 61 | True | True | False |
| 62 | True | True | True |
| 63 | True | True | False |
| 64 | True | True | True |
| 65 | True | True | False |
| 66 | True | True | True |
| 67 | True | True | False |
| 68 | True | True | False |
| 69 | True | True | False |
| 70 | True | True | False |
| 71 | True | True | False |
| 72 | True | True | False |
| 73 | True | True | False |
| 74 | True | True | True |
| 75 | True | True | False |
| 76 | True | True | False |
| 77 | True | True | True |
| 78 | True | False | False |
| 79 | True | True | False |
| 80 | True | True | True |
| 81 | True | True | False |
| 82 | True | True | False |
| 83 | True | True | False |
| 84 | True | True | False |
| 85 | True | True | True |
| 86 | True | False | False |
| 87 | True | True | True |
| 88 | True | True | False |
| 89 | True | True | True |
| 90 | True | True | False |
| 91 | True | True | True |
| 92 | True | True | False |
| 93 | True | True | False |
| 94 | True | True | False |
| 95 | True | True | False |
| 96 | True | False | False |
| 97 | True | True | False |
| 98 | True | True | False |
| 99 | True | True | False |
| 100 | True | False | False |
| 101 | True | True | True |
| 102 | True | True | False |
| 103 | True | True | False |
| 104 | True | True | False |
| 105 | True | True | True |
| 106 | True | True | False |
| 107 | True | True | True |
| 108 | True | False | False |
| 109 | True | True | False |
| 110 | True | True | True |
| 111 | True | True | False |
| 112 | True | True | False |
| 113 | True | True | False |
| 114 | True | True | True |
| 115 | True | True | False |
| 116 | True | False | False |
| 117 | True | False | False |
| 118 | True | True | False |
| 119 | True | True | True |
| 120 | True | True | False |
| 121 | True | True | False |
| 122 | True | True | True |
| 123 | True | True | False |
| 124 | True | True | False |
| 125 | True | True | False |
| 126 | True | False | False |
| 127 | True | True | True |
| 128 | True | False | False |
| 129 | True | True | False |
| 130 | True | True | False |
| 131 | True | True | True |
| 132 | True | True | False |
| 133 | True | False | False |
| 134 | True | False | False |
| 135 | True | True | False |
| 136 | True | True | False |
| 137 | True | True | False |
| 138 | True | True | False |
| 139 | True | False | False |
| 140 | True | True | True |
| 141 | True | True | False |
| 142 | True | True | True |
| 143 | True | True | False |
| 144 | True | False | False |
| 145 | True | True | True |
| 146 | True | True | False |
| 147 | True | True | False |
| 148 | True | True | False |
| 149 | True | True | True |

## Go/No-Go (pre-adjudication)

**Verdict:** NO-GO
- dispatch-selection agreement 26.7% is below the 95.0% threshold
- qwen schema validity 86.0% is below the 95.0% floor — the flip is unsafe regardless of agreement
- qwen latency p95 24134ms exceeds the 5000ms budget

Disagreements require manual adjudication (AC4) before the final 92-2 gate decision — see RouterAdjudication.

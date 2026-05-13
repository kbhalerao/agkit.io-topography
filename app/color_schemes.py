"""
Color ramps for `gdaldem color-relief`.

Format: one entry per line. Each entry is either:
* `nv R G B A`     — no-data color
* `value R G B A`  — exact pixel value
* `pct% R G B A`   — percentile across the band's value range

New since USGS2021: `length_slope` and `slope_steepness` ramps for the
L and LS rasters from GRASS `r.watershed`.
"""

colors = {
    "slope": """nv 0 0 0 0
0, 13, 8, 135, 255
0.25, 65, 4, 157, 255
0.5, 105, 0, 168, 255
0.75, 142, 12, 164, 255
1.0, 174, 40, 146, 255
2.0, 205, 74, 118, 255
3.0, 226, 102, 96, 255
4.0, 243, 133, 75, 255
5.0, 252, 168, 53, 255
10.0, 252, 206, 37, 255
20.0, 240, 249, 33, 255""",

    "elevation": """nv 0 0 0 0
0% 37 52 148 255
16% 41 102 172 255
33% 51 14 188 255
50% 65 182 196 255
66% 129 206 186 255
83% 193 231 188 255
100% 255 255 204 255""",

    "tci": """nv 0 0 0 0
0% 83 41 23 255
66% 102 73 29 255
100% 255 255 15 255""",

    "drainage": """nv 0 0 0 0
1 230 69 69 255
2 230 189 69 255
3 150 230 69 255
4 69 230 109 255
5 69 230 230 255
6 69 109 230 255
7 149 69 230 255
8 230 69 190 255""",

    # NEW — slope length (L). Units are pixels; visual stretch is
    # percentile-based so it works across very different field sizes.
    "length_slope": """nv 0 0 0 0
0% 255 247 236 255
20% 254 232 200 255
40% 253 212 158 255
60% 253 187 132 255
80% 252 141 89 255
90% 239 101 72 255
100% 179 0 0 255""",

    # NEW — slope steepness (LS factor proxy). Log-flavored stretch via
    # percentiles; same intent as slope but covers the longer LS tail.
    "slope_steepness": """nv 0 0 0 0
0% 247 252 245 255
20% 229 245 224 255
40% 199 233 192 255
60% 161 217 155 255
80% 116 196 118 255
90% 65 171 93 255
100% 0 90 50 255""",

    "default": """nv 0 0 0 0
0% 255 255 229 255
12% 247 252 185 255
25% 217 240 163 255
38% 173 221 142 255
50% 120 198 121 255
63% 65 171 93 255
75% 35 132 67 255
88% 0 104 55 255
100% 0 69 41 255""",
}

# Third-Party Notices

This project incorporates or derives from the following third-party works.
Their licenses are reproduced or referenced below.

---

## 1. dankagu-local — protocol definitions (not redistributed)

**Source:** <https://github.com/RainbowUnicorn7297/dankagu-local>
**License:** MIT
**Copyright:** Copyright (c) 2022 RainbowUnicorn7297

The Takasho Protocol Buffers definitions used by this project originate from
the `protos/` directory of the repository above.

**They are not redistributed with this repository.** Both the `.proto` sources
(`protos/`) and the generated Python stubs
(`src/dankagu/grpc/generated/`) are excluded from version control. Users obtain
the schemas directly from the upstream project:

```bash
python scripts/compile_protos.py --fetch
```

This keeps the third-party schemas under their own upstream distribution while
still allowing the project to build. If you redistribute this project, do not
add the schemas to your fork; point users at the command above instead.

Full license text of the upstream project:

```
MIT License

Copyright (c) 2022 RainbowUnicorn7297

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. unity-texture-toolkit — Unity texture / AssetBundle tooling (not redistributed)

**Source:** <https://github.com/esterTion/unity-texture-toolkit>
**License:** MIT
**Copyright:** Copyright (c) esterTion

A Unity `Texture2D` exporter and AssetBundle toolbox, described upstream as
"Texture2D exporter, and other Unity3D bundle toolbox, in PHP". It is a reference
for Unity bundle structure and texture formats (ASTC and raw RGB variants), and
part of the tooling ecosystem this preservation work relies on.

**No code from this project is redistributed here** — nothing under its license is
copied into this repository. It is credited as an acknowledged dependency of the
wider effort.

Full license text of the upstream project:

```
MIT License

Copyright (c) esterTion

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 3. Python runtime dependencies

Runtime and development dependencies are declared in `pyproject.toml` and
pinned in `uv.lock`. Each is distributed under its own license; consult
`uv.lock` or each project's repository for the applicable terms. Principal
dependencies include:

| Package | License |
|---|---|
| FastAPI | MIT |
| Starlette | BSD-3-Clause |
| Uvicorn | BSD-3-Clause |
| grpcio / grpcio-tools | Apache-2.0 |
| protobuf | BSD-3-Clause |
| SQLAlchemy | MIT |
| aiosqlite | MIT |
| Pydantic / pydantic-settings | MIT |
| cryptography | Apache-2.0 / BSD-3-Clause |
| PyJWT | MIT |
| UnityPy | MIT |
| flatbuffers | Apache-2.0 |

---

## 4. Game content and trademarks

"Touhou Project" (東方Project) is the property of Team Shanghai Alice (ZUN).
"Touhou Danmaku Kagura" (東方ダンマクカグラ) and all associated artwork, audio,
music, charts, master data and story text are the property of their respective
rights holders, including the game's original developer and publisher.

No game content is distributed with this repository. Users are expected to
supply their own legally-acquired client and its data. This project is not
affiliated with or endorsed by any of the rights holders.

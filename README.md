# ダンカグ Reborn — Backend Preservation Server

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg" alt="FastAPI"></a>
  <a href="https://grpc.io/"><img src="https://img.shields.io/badge/gRPC-AsyncIO-244c5a.svg" alt="gRPC AsyncIO"></a>
  <a href="https://www.sqlalchemy.org/"><img src="https://img.shields.io/badge/SQLAlchemy-2.0%20Async-d71f00.svg" alt="SQLAlchemy 2.0 Async"></a>
  <a href="https://docs.pydantic.dev/"><img src="https://img.shields.io/badge/Pydantic-v2-e92063.svg" alt="Pydantic v2"></a>
  <a href="https://docs.astral.sh/ruff/"><img src="https://img.shields.io/badge/Code%20Style-Ruff-black.svg" alt="Ruff"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

*An asynchronous, stateful backend server for **Touhou Danmaku Kagura
(東方ダンマクカグラ / ダンカグ)**, written for preservation and study.*

---

## 1. Overview

**Touhou Danmaku Kagura** was a rhythm and mobile gacha game. Its live service was
officially discontinued on **October 28, 2022**.

This project is an independent, async-first implementation of the server side of
that platform, written in Python 3.12+ and managed with **Astral `uv`**. It exists
so the service's behaviour can still be studied and archived after shutdown. It
provides:

- The **LCX REST authentication** surface used before gameplay.
- The **Takasho gRPC** game API, including its custom wire framing.
- An **asset delivery** service for Unity AssetBundles and master databases.
- Stateful **player persistence** through the generic `PlayerStorage` engine.

### Scope

This is a **server-only** project. It performs **no DNS interception and no HTTP
proxying**, and it distributes no client software. It is intended for use with
clients and data that you already lawfully possess.

---

## 2. Architecture

```mermaid
flowchart TD
    Client["Game client (v2.1.0)"]

    subgraph LCX["LCX Authentication — FastAPI/ASGI (443 / 9443)"]
        LCX_API["/environment<br/>/identity/*<br/>/auth/user-access-token<br/>/gcp/analytics/config"]
        JWT["JWT minter<br/>(accessToken, lcxUserIdToken,<br/>signInSessionToken)"]
    end

    subgraph gRPC_Svc["Takasho gRPC — grpc.aio (50051 plaintext, 50052 TLS)"]
        Interceptor["TakashoAsyncInterceptor<br/>(Envoy headers, logging,<br/>auto-mock fallback)"]
        Codec["takasho_unary_handler<br/>+ TakashoPacker"]
        Servicers["26 RPC service namespaces<br/>82 handler bindings"]
    end

    subgraph Assets["Portal and Asset CDN — FastAPI (8080, plain HTTP)"]
        Portal["/ — setup portal<br/>/ca.crt  /server.crt<br/>/status"]
        AssetRouter["/assets/{path}<br/>/ver/{path}  /static/{path}"]
        Generator["Manifest archive synthesis<br/>(arg_m_* / arg_t_*, 7 archives)"]
    end

    subgraph Storage["Persistence"]
        ORM["SQLAlchemy 2.0 Async + asyncpg"]
        DB[("PostgreSQL")]
    end

    Client -->|"1. HTTPS"| LCX_API
    LCX_API --> JWT
    Client -->|"2. gRPC"| Interceptor --> Codec --> Servicers
    Servicers --> ORM
    Client -->|"3. HTTP"| AssetRouter
    Client -.->|"0. fetch CA"| Portal
    Generator --> AssetRouter
    ORM --> DB
```

Four processes run concurrently under one entrypoint, plus an optional gRPC-over-TLS
listener:

| Listener | Default port | Protocol | Purpose |
|---|---|---|---|
| Asset CDN & portal | `8080` | HTTP | Asset streaming, CA delivery, `/status` |
| LCX | `443` | HTTPS | REST authentication |
| LCX (alternate) | `9443` | HTTPS | Same app, unprivileged port |
| Takasho gRPC | `50051` | gRPC (plaintext) | Game RPCs |
| Takasho gRPC TLS | `50052` | gRPC over TLS | Same servicers, over TLS |

Both gRPC listeners expose the identical set of servicers; the plaintext one exists
because TLS is not mandatory for every deployment. The portal deliberately stays
plaintext: a device must be able to reach it *before* it trusts the server
certificate.

---

## 3. Implementation Status

Measured against the shipped code:

| Component | Status |
|---|---|
| Takasho framing codec (cipher + Deflate + HMAC) | Complete, with known-answer tests |
| LCX REST auth surface | 15 route decorators across 20 handlers, including a catch-all fallback |
| gRPC service namespaces registered | **26** |
| Generated RPC methods (from `protos/`) | 113 across 33 service stubs |
| RPC handler bindings wired | 82 (71 distinct method names) |
| RPCs answered by the auto-mock fallback | The remainder — empty but valid responses |
| On-demand manifest generation | 7 archives synthesized at startup |
| Persistence | 3 tables, atomic UPSERT |
| Test suite | 14 tests across 5 modules |

Hand-written source: **5,295 lines across 43 modules** (generated protobuf
stubs — 234 modules, ~11.5k lines — are excluded).

---

## 4. Key Capabilities

**Custom cryptographic transport.** The Takasho platform does not use plain
protobuf on the wire. `core/packer.py` and `core/cipher.py` implement the framing
layer: a 12-byte nonce, a 16-word state block cipher with the platform's own sigma
constant and round-gap table, raw Deflate (`-zlib.MAX_WBITS`), and a 32-byte
HMAC-SHA256 integrity header. The packer accepts a runtime-supplied production key
with a public development key as fallback.

**LCX authentication service.** A FastAPI/ASGI application covering
`/environment`, the `/identity/*` family (sign-in, transfer, link-state,
user-id-token), `/auth/user-access-token`, `/push/*`, `/subs/*`,
`/currency-exchange/*`, `/analytics/*` and `/gcp/analytics/config`. It mints
compliant JWTs (`accessToken`, `lcxUserIdToken`, `signInSessionToken`).
Token signing fails closed if `DANKAGU_JWT_SECRET` is unset.

**Takasho gRPC engine.** A coroutine-based `grpc.aio` server exposing 26 service
namespaces across the `common_featureset` and `fes` families, wired through
`add_generic_rpc_handlers` so every method passes through the packer codec. An
async interceptor injects the Envoy and Takasho metadata headers the protocol
expects. Unregistered RPCs are answered by an auto-mock fallback rather than
failing, so an unknown call returns a valid empty response instead of an error.

**Stateful `PlayerStorage`.** Almost all player state is a key/value entry with
an opaque binary payload. `GetEntriesV2` supports both `EXACT` matching and
`FORWARD` prefix matching; `SetEntriesV2` performs an atomic PostgreSQL upsert, so
concurrent updates cannot violate the composite primary key. Missing keys fall
back to operator-supplied template data.

**Manifest synthesis.** `assets/generator.py` builds the `arg_m_*` / `arg_t_*`
archives needed during boot: FlatBuffers tables wrapped in a single-entry POSIX
TAR, gzipped, then encoded with the platform's lightweight XOR stream cipher.
The TAR is emitted without trailing zero blocks.

**Asset resolution with fallbacks.** Exact path lookup first, then a
manifest-driven catalog resolver that can adapt a same-category AssetBundle for a
missing one, then a placeholder for announcement images that the original service
hosted (those banners were never part of the downloadable asset set). A genuine
miss still returns 404, which is treated as an unrecoverable protocol error by
callers — which is why the `.xab` fallback path matters.

---

## 5. Setup

1. **Install the Root CA via the portal** (only needed if your deployment uses the
   project's self-signed certificate).
   - Browse to `http://<SERVER_LAN_IP>:8080/` and tap **Download DanKagu Root CA**
     (or fetch `http://<SERVER_LAN_IP>:8080/ca.crt` directly).
   - iOS: allow the configuration profile, then install it under
     *Settings → Profile Downloaded* (or *General → VPN & Device Management*).
   - iOS: enable trust under *Settings → General → About → Certificate Trust
     Settings → Enable full trust for root certificates*.

2. **Configure the public hostname.** Set `DANKAGU_PUBLIC_HOST` to the hostname
   this deployment is reachable at, so the generated certificate covers it.

   | Service | Address |
   |---|---|
   | LCX auth | `https://<SERVER_HOST>:443` (or `:9443`) |
   | Takasho gRPC | `<SERVER_HOST>:50051` |
   | Assets | `https://<ASSET_HOST>/assets` |

3. **Start the server** (see [§6](#6-quick-start)).

### Deployment notes

- Serve the asset endpoints from a host with a **publicly trusted certificate**
  if you do not want to install a CA on every device.
- The API host and the asset host may be different names. Keeping them separate is
  the usual arrangement: the API host is the one bound to this server, while the
  asset host is the one that must present a trusted certificate.

---

## 6. Quick Start

```bash
# 1. Environment
uv venv
uv pip install -e ".[dev]"

# 2. Configure secrets and hostname
cp .env.example .env
#    then edit .env — see section 8

# 3. Fetch and compile the protocol schemas
uv run python scripts/compile_protos.py --fetch

# 4. Generate a local CA and server certificate
uv run python -m dankagu.core.certs

# 5. Verify
uv run pytest
uv run ruff check src tests

# 6. Run
uv run python -m dankagu.main
```

Binding `443` requires elevated privileges on most systems; `9443` exists for
the unprivileged case. The entrypoint takes no arguments.

---

## 7. Repository Layout

```
dankagu-reborn/
├── .env.example               # Configuration template (copy to .env)
├── LICENSE                    # MIT, for this project's source only
├── THIRD_PARTY_NOTICES.md     # Third-party licenses and attribution
├── README.md
├── pyproject.toml             # PEP 621 configuration
├── uv.lock                    # Locked dependency graph
├── certs/                     # YOUR generated TLS material (not distributed)
├── data/                      # Runtime data (mostly not distributed)
│   ├── dankagu.db             #   Local SQLite player/state database
│   ├── arg_m_master_dec.db    #   Master database, decoded (supply your own)
│   ├── assets/                #   Indexed AssetBundles and generated manifests
│   ├── hexdata/               #   Response templates (supply your own)
│   ├── manifest/              #   Asset manifest catalogs
│   ├── static/index.html      #   Consent page served to the client WebView
│   └── ver/jp/tos.txt         #   Terms-of-service version marker
├── protos/                    # Third-party schemas, filled by --fetch (not distributed)
├── scripts/
│   └── compile_protos.py      #   Fetch + compile schemas, rewrite imports
├── src/dankagu/
│   ├── config.py              # Pydantic-settings; secrets from the environment
│   ├── main.py                # Orchestrator / entrypoint
│   ├── core/
│   │   ├── cipher.py          #   Takasho 16-word state block cipher
│   │   ├── packer.py          #   Framing codec (nonce + cipher + Deflate + HMAC)
│   │   ├── database.py        #   SQLAlchemy 2.0 Async engine and session
│   │   ├── certs.py           #   Self-signed CA and server certificate generation
│   │   └── net.py             #   LAN IP detection
│   ├── lcx/
│   │   ├── app.py             #   FastAPI app factory and request logging
│   │   ├── router.py          #   Auth and identity routes
│   │   └── tokens.py          #   JWT minting
│   ├── grpc/
│   │   ├── server.py          #   AsyncIO server, servicer registration
│   │   ├── codec.py           #   Packer-aware unary handler wrapper
│   │   ├── interceptors.py    #   Envoy/Takasho metadata + auto-mock fallback
│   │   └── generated/         #   BUILD OUTPUT — regenerate with compile_protos.py
│   ├── services/              #   22 modules registering 26 RPC service namespaces
│   ├── models/                #   SQLAlchemy ORM models
│   └── assets/
│       ├── server.py          #   Asset routes, CA routes, status, fallback resolver
│       ├── generator.py       #   arg_m_* / arg_t_* manifest synthesis
│       └── portal.py          #   Setup portal UI
└── tests/                     # 14 tests across 5 modules
```

---

## 8. Configuration

All configuration comes from environment variables with the `DANKAGU_` prefix,
or from a `.env` file (git-ignored). Real environment variables take precedence
over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `DANKAGU_JWT_SECRET` | *(required)* | HMAC key for LCX JWTs. Token issuance raises until it is set. |
| `DANKAGU_TAKASHO_KEY_HEX` | *(empty)* | 64 hex chars (32 bytes) for the Takasho transport key. If unset, only the public development key is used. |
| `DANKAGU_HOST` | `0.0.0.0` | Bind interface for all listeners. |
| `DANKAGU_LAN_IP` | *(auto)* | Pin the advertised LAN IP; auto-detected when empty. |
| `DANKAGU_PUBLIC_HOST` | *(empty)* | The public hostname of this deployment. Becomes the certificate Common Name and a SAN entry, and is used for absolute URLs. |
| `DANKAGU_PUBLIC_BASE_URL` | *(derived)* | Override the base URL for absolute links served to the client. |
| `DANKAGU_EXTRA_CERT_DNS_NAMES` | *(empty)* | Comma-separated extra SAN entries. |
| `DANKAGU_LCX_IOS_PORT` | `443` | LCX over HTTPS; needs elevated privileges. |
| `DANKAGU_LCX_PORT` | `9443` | LCX over HTTPS, unprivileged. |
| `DANKAGU_GRPC_PORT` | `50051` | Takasho gRPC, plaintext. |
| `DANKAGU_GRPC_TLS_PORT` | `50052` | Takasho gRPC over TLS. |
| `DANKAGU_ASSET_PORT` | `8080` | Asset CDN, portal, `/status`. |
| `DANKAGU_DB_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/dankagu` | SQLAlchemy async PostgreSQL URL (`asyncpg`). |

### Secret handling

The repository contains **no key material** and fails closed rather than
shipping usable defaults:

- `DANKAGU_JWT_SECRET` has no default. Without it, `lcx/tokens.py` raises
  `MissingSecretError` instead of signing with a guessable key.
- `DANKAGU_TAKASHO_KEY_HEX` has no default. The packer falls back to the public
  development key and logs a warning at startup.
- `certs/*`, `.env`, and `*.pem` are git-ignored. Certificates are generated
  locally with `python -m dankagu.core.certs`.

### Data you must supply

These are intentionally not distributed and are git-ignored:

- **TLS material** (`certs/`). Generated locally. The CA certificate is the only
  artifact you ever distribute, and only to your own devices.
- **Protocol schemas** (`protos/`) and the derived stubs
  (`src/dankagu/grpc/generated/`). Third-party content and build output:
  `python scripts/compile_protos.py --fetch`.
- **`data/hexdata/`** — response templates. Eight service modules load from here
  (`wallet`, `player_storage`, `loot_box`, `game_product`, `announcement`,
  `login_bonus`, `reconstruction_spot`, `ondemand_master`). Without them those
  servicers return empty responses; `wallet.py` and `player_storage.py`
  additionally carry small hard-coded fallbacks.
- **`data/arg_m_master_dec.db`** — the master database in decoded form.
- **`data/assets/`, `data/manifest/`** — game assets and manifest catalogs.

---

## 9. Credits

This project would not have been possible without the work of others.

- **[dankagu-local](https://github.com/RainbowUnicorn7297/dankagu-local)** by
  RainbowUnicorn7297 (MIT) — the Takasho Protocol Buffers definitions that this
  server's gRPC surface is built against. They are not redistributed here; see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **[unity-texture-toolkit](https://github.com/esterTion/unity-texture-toolkit)**
  by esterTion (MIT) — a Unity `Texture2D` and AssetBundle toolbox. A valuable
  reference for working with Unity asset bundles and texture formats, and part of
  the wider tooling ecosystem this preservation effort builds on.
- Built with [FastAPI](https://fastapi.tiangolo.com/),
  [gRPC](https://grpc.io/), [SQLAlchemy](https://www.sqlalchemy.org/),
  [Pydantic](https://docs.pydantic.dev/), and
  [Astral `uv`](https://github.com/astral-sh/uv).

---

## 10. Legal Notice

This project is a **non-commercial preservation and research effort**. It is an
independent server implementation for a service that was terminated on
28 October 2022, developed for interoperability with clients that users already
possess. It is **not affiliated with, endorsed by, or sponsored by** Team
Shanghai Alice or the game's original developer and publisher.

- **No game content is distributed here** — no artwork, audio, music, charts,
  story text, master data, or client binaries.
- **No key material is distributed here.** The transport key is read from your
  local environment.
- "Touhou Danmaku Kagura" (東方ダンマクカグラ) and all associated content belong to
  their respective rights holders.
- Provided **as is**, without warranty. You are responsible for complying with
  the laws of your jurisdiction.

If you are a rights holder with a concern, please open an issue.

---

## License

The original source code in this repository is licensed under the
[MIT License](LICENSE). Third-party components keep their own licenses — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The MIT license does not extend
to any game content, trademark, or third-party protocol implementation.

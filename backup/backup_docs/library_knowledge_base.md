# Библиотека фактов — Yevhenii Nam (GG-QandV)

## USER: профиль, опыт, навыки

### Ключевая информация

- **Имя:** Yevhenii Nam (Евгений)
- **Локация:** El Puerto de Santa María, Cadiz, Spain | Remote CET
- **Гражданство:** Украина | Residence & Work Permit: Испания
- **Email:** [маскировано]
- **GitHub:** https://github.com/GG-QandV
- **Telegram:** [маскировано]
- **Роль:** Forward Deployed AI Engineer | AI Automation Architect | AI-Native Product Engineer
- **Занятость:** Фриланс / Контракт

### Языки

- Украинский — Native
- Русский — Native
- Английский — B1/B2 (рабочий: документация, коммуникация, technical writing)
- Польский — C1
- Испанский — A2

### Core Competencies

- Multi-agent AI system design and RAG architecture
- LLM orchestration via n8n, MCP (Model Context Protocol), REST APIs
- Long-context work, agent memory, multi-step delivery pipelines
- Vector databases: Qdrant, Weaviate, PostgreSQL + embeddings
- TypeScript, Node.js, Python, Fastify
- Next.js, React, Tailwind CSS
- Docker, VPS, Coolify, Portainer
- Supabase, PostgreSQL, SQLite (WAL)
- n8n (advanced custom workflows beyond built-in nodes)
- RLHF, SFT data preparation, response ranking
- Prompt Engineering: multi-step tasks, edge cases, safety testing
- LLM APIs: OpenAI, Claude (incl. MCP & Claude Code), Gemini, Grok, Perplexity
- Local models: ONNX, TEI (multilingual-e5-small)

### Forward Deployment Strengths

- Systems thinking and structured problem solving
- Discovery and requirements clarification in ambiguous environments
- Business-process analysis and workflow mapping
- Translating business needs into technical architecture and delivery plans
- End-to-end ownership: discovery, scoping, implementation, testing, deployment, iteration
- AI-native product development and practical AI adoption
- Integration-first mindset: APIs, data flows, operational systems, process automation
- Product judgement: prioritisation, trade-offs, MVP scoping
- Independent execution, rapid learning and adaptation to new domains
- Production reliability, operational pragmatism, accountable delivery

### Experience Timeline

- **2022–Present** — Forward Deployed AI Engineer / AI Product Engineer (Freelance, Spain)
  - LLM evaluation under NDA: Scale AI, Outlier, Meta AI
  - RLHF, SFT, response ranking, golden answer creation, safety testing, prompt stress-testing
  - Multi-agent AI systems, API/webhook integrations, agent workflows
  - Deployment: Docker, VPS, self-hosted services, data stores
- **2012–2022** — Co-Founder / Head of Marketing & Partner Relations (Modern Interior, Kyiv)
  - Built and scaled business from zero
  - End-to-end delivery: concept → commercial proposal → negotiation → launch
  - Cross-functional team management: architects, designers, construction teams
- **2008–2012** — Real Estate Agent (Kyiv)
- **2003–2007** — Head of Consumer Lending Department (Top-10 Bank, Kyiv)
  - Built consumer lending division from scratch across 90+ retail branches
  - Developed lending methodology, risk frameworks, operational documentation
- **2000–2003** — Director, Credit Company (Private Finance Institution, Kyiv)
  - Architected and operationalized consumer finance organization from zero
  - Corporate turnaround: loss-making to profitable
- **1997–2000** — Motion Designer / Post-Production Specialist (Warsaw, Poland)
  - Visual effects, motion graphics, video editing

### Education

- **2003** — Master's in Financial Management & Marketing (Kyiv National Economic University)
- **2024–2025** — Courses: Original Content Creation (fiction/non-fiction books & screenplays)
- **1987–1990** — Certificate in Contemporary Painting & Drawing (4 years)

### Publications

- *Simple Mediterranean Diet Cookbook for Beginners* — English, 230+ pages, Amazon (March 2025)

---

## PROJECT: Mnemostroma — Memory Layer for AI Agents

**GitHub:** https://github.com/GG-QandV/mnemostroma  
**License:** FSL (Functional Source License)  
**Status:** PRODUCTION — v1.7.5-alpha, 403/403 tests passing

### What it does

Production-ready memory layer for LLM agents. Prevents context loss, session fragmentation, and stale data in agent-heavy production systems.

### Technical specs

- Offline, RAM-first (~600MB footprint, ~80ms retrieval), no GPU required
- Dual-stream async pipeline (Observer + Content)
- 5-layer memory dissolution model
- numpy MatrixSearch ANN, ONNX INT8 inference
- SQLite WAL persistence
- Full MCP integration: Claude Desktop, Claude Code, Cursor, Windsurf, Zed, Google Antigravity, and other IDEs
- Solo developer project — architecture, implementation, testing, documentation

### Use cases

- AI agent persistent memory across sessions
- Context management for production agent systems
- Reducing repeated work and context loss in agent conversations

---

## PROJECT: Context Manager — Hybrid Synchronized Database

**GitHub:** https://github.com/GG-QandV/context-manager  
**License:** Apache 2.0  
**Status:** PRODUCTION — v2.2.1 (Windows, Linux, macOS)

### What it does

AI agent context orchestration service: bridge between structured PostgreSQL data and Qdrant's high-performance vector search. Unified storage, retrieval, and plain-language access to all structured and unstructured company data.

### Technical specs

- Dual storage: PostgreSQL (structured) + Qdrant (vector search), auto-sync
- Local embeddings: multilingual-e5-small via TEI (no cloud, no API keys)
- MCP-native: speaks Model Context Protocol out of the box — Claude Desktop, Antigravity, Cursor connect directly
- Self-healing watchdog: monitors every component, restarts what breaks
- Fastify REST API with 9 MCP tools
- Windows: one-click PowerShell install, system tray icon, nssm services
- Linux: Docker Compose

### MCP Tools

- `cm_save_br` / `cm_save_im` / `cm_save_fl` — save context (brief/topic/full)
- `cm_search` — semantic search in your context
- `cm_query` — search by date, agent, session
- `cm_cross` — search another agent's context
- `cm_agents` — list all agents
- `cm_stats` / `cm_export` — statistics and export

### Architecture

```
src/
├── services/    Core: Qdrant, PostgreSQL, sync, embeddings
├── routes/      API route handlers
├── schemas/     Validation schemas (TypeBox)
├── config/      Paths, migration, env
├── types/       TypeScript types
mcp/             MCP stdio server + HTTP adapter
scripts/         Install/uninstall/MCP config generation
```

---

## PROJECT: μ Gate — AI Agent Payment Guard (agents_blockchain)

**GitHub:** https://github.com/GG-QandV/agents-blockchain  
**License:** BSL 1.1  
**Status:** PRODUCTION — v0.2.1  
**Language:** Rust (~15 crates)

### What it does

Local daemon that controls AI-agent payments without exposing wallet keys. The agent sends payment intents over a Unix socket; μ Gate checks them against owner-defined rules before signing.

### Key features

- Zero gas fees for the owner — payments use x402 (EIP-3009 TransferWithAuthorization) over USDC on Base
- Rules engine: daily budget, recipient whitelist, per-resource price caps, biometric confirmation threshold
- Every decision and payment logged in a tamper-evident append-only hash chain
- Ed25519-signed license check

### Architecture

```
Agent → μ Gate (localhost unix socket) → Ω-check (connector, ceiling) → Δ-check (whitelist, daily budget) → [biometric] → WAL (fsync) → sign EIP-3009 → retry with X-PAYMENT → status → Agent
```

### Crate modules

| Crate               | Role                                               |
| ------------------- | -------------------------------------------------- |
| `mu-gate`           | Socket server, ed25519 auth, rate-limit, allowlist |
| `mu-runtime`        | Pipeline: Ω → Δ → [human] → WAL → execute          |
| `mu-connect/x402`   | x402 client (EIP-712, EIP-3009)                    |
| `mu-policy`         | Ω (ceiling) + Δ (whitelist, budget, resources)     |
| `mu-log`            | Append-only hash chain WAL                         |
| `mu-vault`          | k256/P-256 keys, soft vault / enclave              |
| `mu-human`          | Owner biometric confirmation dialog                |
| `mu-core`           | μ-object format, CBOR-like serialization           |
| `composer-core/cli` | Offline policy change proposals                    |

### Configuration (policy.toml)

```toml
daily_limit_minor = 5_000_000_000       # $5000 USDC/day
confirm_threshold_minor = 100_000_000   # >$100 → owner confirms
[[whitelist]]                           # approved recipient addresses
[[resource_allowlist]]                  # per-API price caps
```

---

## PROJECT: fammy.pet — B2C SaaS

**Live:** https://fammy.pet  
**Status:** PRODUCTION — core features live, active development  
**Stack:** TypeScript, Node.js/Fastify, Next.js/React, Tailwind CSS, Supabase  
**Deployment:** VPS via Docker/Coolify

### What it does

B2C SaaS product for pet nutrition. Independently designed, built, and deployed. Stripe payments integrated, UI 100% complete, 2 core features live.

---

## PROJECT: PetSafe Validator (Nutrition_Nutrients)

**Status:** PRODUCTION — active upgrade cycle  
**Stack (current):** NestJS + Fastify + TypeScript + Zod + PostgreSQL  
**Stack (target):** FastAPI + Python 3.11 + Supabase + Stripe + Sentry + Vercel/Railway

### What it does

Backend API for pet nutrition validation with household multi-tenancy.

### Core modules

- `auth` — JWT authentication
- `users` — User profiles
- `households` — Multi-tenant structure with member invitations
- `subjects` — CRUD for pets
- `billing` — Stripe subscriptions, promo codes, webhook queue
- `functions` — F1-F6 nutrition tools
  - **F1:** Diet Validator — comprehensive diet analysis
  - **F2:** Food Check — safety checker for human foods
  - **F3:** Portion Calculator — accurate portion sizes
  - **F4:** Recipe Generator — balanced recipe formulation
  - **F5:** BCS Tracker — Body Condition Score tracking
  - **F6:** Nutrient Advisor — detailed nutrient guidance
- `notifications` — In-app notifications
- `reference` — Master data (food, nutrients, targets)
- `i18n` — Multi-language: en, ua, es, fr
- `admin` — Operational endpoints
- `push` — Mobile push notifications

### API groups

`/api/v1/auth/*`, `/api/v1/users/*`, `/api/v1/households/*`, `/api/v1/invitations/*`, `/api/v1/subjects/*`, `/api/v1/functions/*`, `/api/v1/billing/*`, `/api/v1/i18n/*`, `/api/v1/push/*`, `/api/v1/reference/*`, `/api/v1/admin/*`

---

## PROJECT: speech-local v2.0

**Status:** ACTIVE DEVELOPMENT — final stage  
**Stack:** Python 3.12+ (asyncio), aiohttp, whisper.cpp  
**Концепция:** Offline-first speech translation & draft assistant для Zoom/Google Meet/MS Teams.

### Архитектура

Два трека обработки:

- **Точный трек** — локальный whisper.cpp → raw_text (неизменяем) → перевод через LLM (Gemini/Claude/Custom)
- **Быстрый трек** — частичные результаты → облачный realtime (только в открытом профиле)

Профили конфиденциальности: **открытый** (аудио + текст в облако) / **конфиденциальный** (только текст, локальный STT работает всегда).

### Pipeline

```
PipeWire (pw-record/FFmpeg) → VAD → сегментация → whisper.cpp → raw_text (SQLite) → перевод (LLM) → UI (SSE)
```

### Components

- Audio: PipeWire discovery, захват 2 потоков, VAD, сегментация по паузе
- STT: whisper.cpp (base/tiny fallback), очередь, каскад деградации
- Translation: Gemini/Claude/Custom HTTP + OpenAI Realtime, 3 режима (literal/safe/post_clean), динамический контекст
- Drafts: I1-I5 — библиотека фактов → детект вопроса → генерация → guardrails → перевод
- Security: BYOK (ключи в RAM 60 мин), LogRedactor
- UI: SSE, 3 вкладки (перевод, черновики, диагностика), topbar с профилем
- Watchdog: cgroup memory, каскад деградации L0-L3
- **275 тестов, все проходят**

---

## PROJECT: Hermes — Agent Workspace

**Status:** BETA  
**GitHub:** https://github.com/GG-QandV/HERMES

### What it does

Full agent workspace: конфигурация, скрипты, планы, бекапы для агента Hermes.

### Components

- **Mirror Channel** — дуплекс CLI↔TG, spec готов
- **Telegram Gateway** — systemd сервис, polling режим
- **Model:** DeepSeek V4 Flash (основная), Gemini 2.5 Flash (subagents)
- **OpenCode** — бесплатный агент для массовой работы

---

## SERVICE: Crawl4AI (fork/использование)

**GitHub:** https://github.com/unclecode/crawl4ai (50k+ stars)  
**License:** Apache 2.0  
**Stack:** Python, Playwright, Docker

### What it does

Open-source LLM-friendly web crawler & scraper. Turns the web into clean, LLM-ready Markdown for RAG, agents, and data pipelines.

### Key features

- **Markdown Generation** — clean Markdown with headings, tables, code, citations
- **Structured Data Extraction** — LLM-driven, CSS-based, JSON schema
- **Browser Integration** — managed browser, remote control, session management, proxy support
- **Anti-Bot Detection** — 3-tier detection with proxy escalation
- **Deep Crawl** — BFS/DFS/BestFirst with crash recovery and resume_state
- **Docker Deployment** — real-time monitoring dashboard, browser pooling, MCP integration
- **Undetected Browser** — bypass Cloudflare, Akamai, custom bot detection

### Use in pipelines

- Web → clean Markdown → RAG ingestion
- Deep crawl → structured data extraction → analytics
- LLM extraction with any provider (OpenAI, Claude, Gemini, local via Ollama)

---

## SERVICE: MarkDownload (fork/CLI update)

**GitHub:** https://github.com/deathau/markdownload  
**Stack:** Browser Extension (JS, Readability.js, Turndown)

### What it does

Browser extension to clip websites and download them as readable Markdown files.

### Key features

- One-click page → Markdown via browser icon
- Uses Mozilla Readability.js (Firefox Reader View engine) + Turndown (HTML→MD)
- Download full page or selected text as .md
- Obsidian integration via clipboard
- Available: Firefox, Chrome, Edge, Safari
- **CLI update made by user** for agent-based workflows

### Use in pipelines

- Web → browser → Markdown → knowledge base
- Research capture → Obsidian vault

---

## SERVICE: OCR Pipeline (Zerox fork)

**GitHub:** https://github.com/getomni-ai/zerox  
**Stack:** Python/Node.js, vision LLMs, poppler/graphicsmagick

### What it does

Dead-simple OCR for AI ingestion: PDF/DOCX/image → images → vision LLM → Markdown.

### Pipeline

```
PDF/DOCX/image → libreoffice → images → GPT-4o/Claude/Gemini → Markdown
```

### Supported file types

pdf, doc, docx, odt, rtf, txt, html, xls, xlsx, csv, ppt, pptx, and more — 22 formats total

### Supported models

- OpenAI: GPT-4o, GPT-4o-mini, GPT-4.1
- Azure OpenAI
- AWS Bedrock: Claude 3 Haiku/Sonnet/Opus
- Google Gemini: 1.5 Flash/Pro, 2.0 Flash/Flash-Lite
- Anthropic Claude

### Use in pipelines

- Document → Markdown → RAG
- Invoice/contract OCR → structured data extraction
- Batch document processing with concurrency

---

## SERVICE: SQL Deduplicator (Splink)

**GitHub:** https://github.com/moj-analytical-services/splink  
**Stack:** Python, DuckDB/Spark/PostgreSQL  
**License:** MIT

### What it does

Fast, accurate, and scalable data linkage and deduplication. Probabilistic record linkage (Fellegi-Sunter model) — deduplicate and link records that lack unique identifiers.

### Key features

- Speed: million records on laptop in ~1 minute
- Backends: DuckDB (local), Spark (100M+ records), PostgreSQL
- Unsupervised — no training data required
- Interactive visualisations for model understanding
- Term frequency adjustments, fuzzy matching

### Use in pipelines

- Customer deduplication across databases
- Entity resolution for data integration
- Record linkage for analytics/reporting

---

## SERVICE: Lumina — Agentic Desktop Assistant (agen-loop)

**GitHub:** https://github.com/kamedashe/Lumina  
**Stack:** Rust + Tauri v2, React 19 + TypeScript, Vite 6  
**Status:** Open source reference

### What it does

Agentic AI desktop assistant with multi-provider support, real-time streaming, native tool-calling.

### Features

- **Multi-Provider:** Ollama (local), OpenAI-compatible, Anthropic Claude, Google Gemini
- **Agentic Tool-Calling:** real agent loop — list_files, read_file, write_file, get_current_dir, list_processes, search_documents
- **RAG with Pluggable Vector Store:** sqlite-vec (local), SQLite (fallback), Pinecone (cloud)
- **Streaming:** SSE/NDJSON over Tauri IPC
- **Plugin System:** QuickJS sandbox for JavaScript plugins
- **Modern UI:** light/dark themes, CSS variables, Acrylic backdrop

### Use as reference

- Architecture pattern for agent loop (tool-calling → execute → feed back → continue)
- Multi-provider LLM client architecture
- Desktop app with Rust backend + React frontend

---

## SERVICE: n8n AI Workflow — Amazon Book Pipeline

**Live:** https://a.co/d/6xAXiaT  
**Published:** March 2025

### What it does

5-agent n8n workflow for end-to-end book production: scraping/parsing → translation/adaptation → versioning/editing → proofreading/formatting → marketing/SEO → publication.

### Output

*Simple Mediterranean Diet Cookbook for Beginners* — 230+ pages, English, self-published on Amazon.

### Pipeline stages

1. Scraping/parsing (web → structured data)
2. Translation/adaptation (content optimization)
3. Versioning/editing (multi-pass editorial)
4. Proofreading/formatting (final polish)
5. Marketing, SEO → publication

---

## YOU: situational context

### Как работать с этим документом

- Файл для библиотеки фактов проекта speech-local (FactLibrary)
- use case: генератор черновиков ответов (I2) использует эту справку для ответа на вопросы о пользователе
- domain: AI/ML, software engineering, automation, agent systems
- generate_language: ru (черновики на русском)
- источники: указывать название проекта/сервиса

### Ключевые проекты для упоминания в ответах

1. **Mnemostroma** — memory layer for AI agents (most production-ready)
2. **Context Manager** — PostgreSQL + Qdrant sync (most deployed, 3 OS)
3. **μ Gate** — AI agent payment guard (most innovative, Rust)
4. **fammy.pet** — full-cycle B2C SaaS (most entrepreneurial)
5. **PetSafe Validator** — biggest domain complexity (pet nutrition, multi-tenant)
6. **speech-local** — most recent, final dev stage (offline speech translation)
7. **Crawl4AI** — used/integrated, 50k+ stars community crawler
8. **Splink** — used for record linkage/deduplication
9. **Zerox OCR** — used for document → Markdown pipelines
10. **Lumina** — reference architecture for agent loops

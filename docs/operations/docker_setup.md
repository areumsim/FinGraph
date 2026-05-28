# Docker 셋업 가이드 (PG + Neo4j minimal)

AutoNexusGraph 의 **PostgreSQL(pgvector)** + **Neo4j 5.18** 컨테이너 셋업.
Qdrant/Redis 는 옵션 (compose 에 주석으로 슬롯만 남김).

## 포트 / 경로 / 컨테이너명 한눈에

| 서비스 | 컨테이너명 | 호스트 포트 → 컨테이너 내부 | 호스트 볼륨 → 컨테이너 내부 |
|---|---|---|---|
| PostgreSQL | `ar-postgres` | 31011 → 5432 | `${DB_DATA_ROOT}/postgres` → `/var/lib/postgresql/data` (실데이터는 `…/postgres/pgdata/`) |
| Neo4j | `ar-neo4j` | 31009 → 7474 (HTTP)<br>31010 → 7687 (Bolt) | `${DB_DATA_ROOT}/neo4j/data` → `/data`<br>`${DB_DATA_ROOT}/neo4j/logs` → `/logs`<br>`${DB_DATA_ROOT}/neo4j/import` → `/var/lib/neo4j/import`<br>`${DB_DATA_ROOT}/neo4j/plugins` → `/plugins` |

`DB_DATA_ROOT` 기본값: `/home/user/arsim/DB_FG` (AutoNexusGraph 전용 — 다른 프로젝트와 분리).

## 시나리오 A: 호스트에서 직접

```bash
# 1. 데이터 폴더 사전 생성 (docker 가 자동 생성하지만 권한 통제 위해 권장)
mkdir -p ~/arsim/DB_FG/{postgres,neo4j/data,neo4j/logs,neo4j/import,neo4j/plugins}

# 2. (필요 시) DB_DATA_ROOT override
export DB_DATA_ROOT=~/arsim/DB_FG    # 기본값과 동일이면 생략

# 3. 기동
cd AutoNexusGraph
docker compose up -d                     # 둘 다
docker compose up -d postgres            # PG 만
docker compose up -d neo4j               # Neo4j 만

# 4. 로그
docker compose logs -f --tail=50 postgres

# 5. 정지 / 제거
docker compose stop                      # 컨테이너만 멈춤 (데이터 유지)
docker compose down                      # 컨테이너 제거 (데이터는 호스트 폴더에 유지)
docker compose down -v                   # 데이터까지 삭제 (주의 — bind mount 라 호스트 폴더는 안 지워짐)
```

스키마 자동 적용: PG 가 빈 데이터 디렉토리로 첫 기동하면 `infra/postgres/init/01_schema.sql` 실행.
이미 데이터가 있으면 재실행 안 됨 (의도된 동작 — 데이터 보존).

**.env 접속 설정:**
```env
NEO4J_URI=bolt://192.168.88.201:31010
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@192.168.88.201:31011/fingraph
DB_DATA_ROOT=/home/user/arsim/DB_FG
```

---

## 시나리오 B: dev 컨테이너 안에서 작업

(현재 ar-poc-dev 같은 환경 — dev 컨테이너 안에서 코드 작업, DB 컨테이너는 호스트에서 띄움.)

dev 컨테이너에 docker 클라이언트가 없거나 `/var/run/docker.sock` 미마운트 → 호스트에서 띄워야 합니다.

### B-1) 호스트에 SSH 해서 compose 기동

```bash
ssh -p 31001 root@192.168.88.201
cd /home/user/arsim/AutoNexusGraph
mkdir -p ~/arsim/DB_FG/{postgres,neo4j/data,neo4j/logs,neo4j/import,neo4j/plugins}
docker compose up -d
```

### B-2) dev 컨테이너에서 호스트 IP+포트로 접속 (기본)

dev 컨테이너의 `.env`:
```env
NEO4J_URI=bolt://192.168.88.201:31010
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@192.168.88.201:31011/fingraph
```

### B-3) 같은 docker network 에 join (선택 — 컨테이너명으로 통신)

dev 컨테이너가 `ar-poc-network` 에 있다면 AutoNexusGraph compose 도 join.

`docker-compose.yml` 의 `networks` 블록 수정:
```yaml
networks:
  ar-poc-network:
    external: true
```

각 서비스에 추가:
```yaml
services:
  postgres:
    networks: [ar-poc-network]
  neo4j:
    networks: [ar-poc-network]
```

dev 컨테이너의 `.env` 변경:
```env
NEO4J_URI=bolt://ar-neo4j:7687           # 컨테이너명 + 내부 포트
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@ar-postgres:5432/fingraph
```

---

## 시나리오 C: compose 없이 docker run

```bash
# PostgreSQL (pgvector 내장)
docker run -d --name ar-postgres \
  --restart unless-stopped \
  -p 31011:5432 \
  -e POSTGRES_USER=fingraph \
  -e POSTGRES_PASSWORD=fingraph_dev \
  -e POSTGRES_DB=fingraph \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v $(pwd)/infra/postgres/init:/docker-entrypoint-initdb.d:ro \
  -v ~/arsim/DB_FG/postgres:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

# Neo4j 5.18
docker run -d --name ar-neo4j \
  --restart unless-stopped \
  -p 31009:7474 -p 31010:7687 \
  -e NEO4J_AUTH=neo4j/fingraph_dev \
  -e 'NEO4J_PLUGINS=["apoc"]' \
  -v ~/arsim/DB_FG/neo4j/data:/data \
  -v ~/arsim/DB_FG/neo4j/logs:/logs \
  -v ~/arsim/DB_FG/neo4j/import:/var/lib/neo4j/import \
  -v ~/arsim/DB_FG/neo4j/plugins:/plugins \
  neo4j:5.18-community
```

같은 network join 시 `--network ar-poc-network` 추가.

---

## 헬스체크

기동 후 30~60초 대기:

```bash
make health
```

기대 출력 (정상):
```
neo4j         OK        ping ok
postgres      OK        ping ok
pgvector      OK        vector extension installed
qdrant        SKIP      QDRANT_URL 미설정 (minimal 스택 — pgvector 사용)
embedding     FAIL      ...                   # GPU 컨테이너 후속 PR
dart          OK        company.json status=000 (삼성전자(주))
ecos          SKIP      ECOS_API_KEY 미설정
```

---

## 적재 (PG)

```bash
make load-companies     # 295 rows
make load-filings       # 4,584 rows
make load-financials    # 184,199 rows (수 분)
# 또는
make load-all
```

idempotent (UPSERT) — 여러 번 실행 안전.

---

## 트러블슈팅

### "Cannot connect to the Docker daemon"
→ dev 컨테이너 안엔 docker 클라이언트 없을 가능성. 시나리오 B-1 (호스트 SSH).

### Neo4j connection refused
→ 첫 기동 30~60초 걸림. `docker logs ar-neo4j | grep "Remote interface available"` 확인.

### PG schema 미적용
→ 볼륨에 데이터가 이미 있으면 init 안 돌아감. 완전 초기화:
```bash
docker compose down
rm -rf ~/arsim/DB_FG/postgres/*
docker compose up -d postgres
```

### `vector` 확장 없음
→ `postgres:16-alpine` 쓰면 pgvector 없음. 우리 compose 는 `pgvector/pgvector:pg16` 사용. 이미지 확인:
```bash
docker inspect ar-postgres | grep Image
```

### 포트 충돌
→ 31xxx 가 다른 컨테이너와 충돌하면 docker-compose.yml 의 ports 매핑 수정. dev (31001-31008), DB (31009-31011) 가 기본.

### bind mount 권한
→ Neo4j/PG 가 디렉토리 소유권 문제로 실패하면:
```bash
sudo chown -R 7474:7474 ~/arsim/DB_FG/neo4j   # Neo4j 5 컨테이너 UID
sudo chown -R 999:999 ~/arsim/DB_FG/postgres  # postgres UID
```

### 데이터 분리 (다른 프로젝트와 섞이지 않게)
→ 이미 `DB_FG` 폴더로 분리. 다른 프로젝트가 `~/arsim/DB` 쓰면 영향 없음.

---

## 임베딩 (BGE-M3 / Reranker) — dev container 안에서 GPU 사용

dev container (`ar-poc-dev-docker`) 가 GPU(1,3) + 64GB RAM 보유 → docker-in-docker 없이 직접 실행:

```bash
# 의존성 (최초 1회)
pip install sentence-transformers fastapi 'uvicorn[standard]' torch

# 기동 (둘 다)
python scripts/serve_embeddings.py
# → http://0.0.0.0:8080  (BGE-M3 embed)
# → http://0.0.0.0:8081  (BGE-Reranker)

# 임베딩만
python scripts/serve_embeddings.py --no-rerank

# CPU 강제 (테스트용)
python scripts/serve_embeddings.py --device cpu
```

엔드포인트는 TEI(text-embeddings-inference) 와 동일 schema → `src/autonexusgraph/embeddings.py` 클라이언트 변경 불필요.

`.env`:
```env
EMBEDDING_URL=http://localhost:8080
RERANKER_URL=http://localhost:8081
EMBEDDING_DIM=1024
```

자원 사용:
- BGE-M3: GPU ~2.5GB / RAM ~2GB
- Reranker-v2-m3: GPU ~1.5GB
- 단일 GPU 4GB면 둘 다 충분

별도 컨테이너로 띄우고 싶으면 docker-compose.yml 의 `bge-m3 / bge-reranker` 주석 해제 (NVIDIA Container Toolkit 필요).

---

## src/ 컨테이너화 (후속)

현재 dev container 안에서 직접 실행 중. 본격 운영 시 docker-compose.yml 의 `api / web / ingestion-worker` 슬롯 주석 해제:

```yaml
api:
  build:
    context: .
    dockerfile: infra/Dockerfile          # 후속 PR
  command: ["uvicorn", "autonexusgraph.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
  ports: ["31020:8000"]
  ...
```

지금은 호스트/dev 에서 직접:
```bash
# API
uvicorn autonexusgraph.api.main:app --reload --port 8000

# Streamlit UI
streamlit run src/autonexusgraph/ui/app.py --server.port 8501
```


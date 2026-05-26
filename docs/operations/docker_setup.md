# Docker 셋업 가이드

FinGraph 의 Neo4j / PostgreSQL / Qdrant 컨테이너를 띄우는 3가지 시나리오.

## 시나리오 A: 호스트에서 일반 실행 (가장 단순)

작업환경이 호스트(머신) 자체이거나, docker-in-docker 가능한 곳에서:

```bash
cd FinGraph

# 전체 한 번에
docker compose up -d

# 또는 개별
docker compose up -d postgres        # PG 만
docker compose up -d neo4j           # Neo4j 만
docker compose up -d qdrant          # Qdrant 만

# 로그
docker compose logs -f --tail=50 postgres

# 정지 (데이터 유지)
docker compose stop

# 완전 제거 (볼륨 포함 = 데이터 삭제)
docker compose down -v
```

**.env 접속 설정:** host 의 1xxxx 포트로 접근.
```env
NEO4J_URI=bolt://localhost:17687
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@localhost:15432/fingraph
QDRANT_URL=http://localhost:16333
```

스키마는 첫 기동 시 `infra/postgres/init/01_schema.sql` 자동 적용.

---

## 시나리오 B: dev container 에서 별도 compose 띄울 때

(현재 작업환경 같은 케이스 — dev container 안에서 작업, PG/Neo4j 를 별도 컨테이너로 띄움.)

dev container 는 보통 docker 클라이언트 없거나 `/var/run/docker.sock` 미마운트 → **호스트에서 띄워야** 합니다.

### 1) 호스트로 SSH 들어가서

```bash
ssh -p 31001 root@192.168.88.201           # 사용자 호스트 예시
cd /home/user/arsim/FinGraph               # 호스트의 마운트 경로
docker compose up -d postgres neo4j qdrant
```

### 2) dev container 와 PG/Neo4j 가 같은 docker network 에 있도록 설정

dev container 가 `ar-poc-network` 에 있다면, FinGraph compose 도 join:

`docker-compose.yml` 의 `networks` 블록을:
```yaml
networks:
  fingraph_net:
    driver: bridge
  ar-poc-network:
    external: true
```

각 서비스에 추가:
```yaml
services:
  postgres:
    networks: [fingraph_net, ar-poc-network]
  neo4j:
    networks: [fingraph_net, ar-poc-network]
  qdrant:
    networks: [fingraph_net, ar-poc-network]
```

그러면 dev container 안에서:
```env
NEO4J_URI=bolt://fingraph-neo4j:7687       # 컨테이너명 + 내부 포트
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@fingraph-postgres:5432/fingraph
QDRANT_URL=http://fingraph-qdrant:6333
```

### 3) 또는 호스트 IP + 매핑 포트 (network join 안 한 경우)

```env
NEO4J_URI=bolt://192.168.88.201:17687
POSTGRES_DSN=postgresql://fingraph:fingraph_dev@192.168.88.201:15432/fingraph
QDRANT_URL=http://192.168.88.201:16333
```

---

## 시나리오 C: compose 없이 docker run 만으로

compose 가 부담스러운 환경:

```bash
# PostgreSQL
docker run -d --name fingraph-postgres \
  --restart unless-stopped \
  -p 15432:5432 \
  -e POSTGRES_USER=fingraph \
  -e POSTGRES_PASSWORD=fingraph_dev \
  -e POSTGRES_DB=fingraph \
  -v $(pwd)/infra/postgres/init:/docker-entrypoint-initdb.d:ro \
  -v fingraph_pg_data:/var/lib/postgresql/data \
  postgres:16-alpine

# Neo4j 5.18
docker run -d --name fingraph-neo4j \
  --restart unless-stopped \
  -p 17474:7474 -p 17687:7687 \
  -e NEO4J_AUTH=neo4j/fingraph_dev \
  -e 'NEO4J_PLUGINS=["apoc"]' \
  -v fingraph_neo4j_data:/data \
  -v fingraph_neo4j_logs:/logs \
  -v fingraph_neo4j_import:/var/lib/neo4j/import \
  -v fingraph_neo4j_plugins:/plugins \
  neo4j:5.18-community

# Qdrant
docker run -d --name fingraph-qdrant \
  --restart unless-stopped \
  -p 16333:6333 -p 16334:6334 \
  -v fingraph_qdrant_data:/qdrant/storage \
  qdrant/qdrant:v1.9.0
```

같은 network 에 join 하려면 각 명령에 `--network ar-poc-network` 추가.

---

## 헬스체크

기동 후 30~60초 기다린 뒤:

```bash
make health         # 또는 python scripts/healthcheck.py
```

기대 출력:
```
neo4j         OK        ping ok
postgres      OK        ping ok
qdrant        OK        ping ok
embedding     FAIL      ...               # 임베딩 컨테이너는 후속 PR
dart          OK        company.json ...
ecos          SKIP      ECOS_API_KEY 미설정
```

---

## 트러블슈팅

### "Cannot connect to the Docker daemon"
→ dev container 안에선 docker 클라이언트가 없을 가능성. 시나리오 B 의 방법으로 호스트에서 실행.

### Neo4j 가 connection refused
→ 첫 기동 시 30~60초 걸림. `docker logs fingraph-neo4j` 로 `Remote interface available at` 확인 후 재시도.

### PG schema 가 적용 안 됨
→ 볼륨이 이미 존재하면 init 스크립트가 다시 안 돌아감. 완전 재초기화:
```bash
docker compose down -v          # 또는: docker volume rm fingraph_pg_data
docker compose up -d postgres
```

### 포트 충돌
→ 호스트에 이미 PG/Neo4j 가 있으면 docker-compose.yml 의 외부 포트(17xxx, 15432, 16333)를 다른 값으로 변경.

### dev container 에서 컨테이너명 접속 안 됨
→ 같은 docker network 에 join 안 됨. 시나리오 B-2 의 networks 설정 확인. 또는 시나리오 B-3 의 호스트 IP 방식.

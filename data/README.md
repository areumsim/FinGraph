# data/

AutoNexusGraph 데이터 작업 디렉토리. 본 폴더 내부 파일은 git 추적 외 (`.gitignore`).
폴더 골격만 `.gitkeep` 으로 유지.

## 레이아웃

```
data/
├── raw/                              # 원본 다운로드 — 변경 금지
│   ├── dart/
│   │   ├── corpCode.xml.zip          # 회사 코드 마스터
│   │   ├── corp_codes_listed.csv     # 상장사만 평탄화
│   │   ├── reports/<corp_code>/      # 사업/반기/분기 보고서 메타·원문
│   │   │   ├── filings.jsonl
│   │   │   └── documents/<rcept_no>.zip
│   │   └── financials/<corp_code>/   # XBRL 재무제표
│   │       └── <year>_<reprt>_<fs_div>.jsonl
│   ├── krx/
│   │   ├── listings_all.csv          # 전체 상장 종목
│   │   └── index_kospi200.csv        # 지수 구성
│   └── ecos/
│       └── base_rate.jsonl
│
└── processed/                        # 전처리 산출 (청크, 정규화 결과)
    ├── chunks/<corp_code>/<rcept_no>.jsonl
    └── financials_normalized.parquet
```

## 다운로드 명령

```bash
# 1. 회사 코드 마스터 (필수, 최초 1회)
make ingest-corp

# 2. KRX 상장사 + 지수 구성
make ingest-krx

# 3. ECOS 거시지표
make ingest-ecos

# 4. 일괄
make ingest-all
```

세부 인자:
```bash
python scripts/ingest/download_corp_codes.py --help
python scripts/ingest/download_listings.py --markets KOSPI,KOSDAQ
python scripts/ingest/download_business_reports.py --corp-code 00126380 --start 20220101 --end 20241231
python scripts/ingest/download_financials.py --corp-codes-csv data/raw/dart/corp_codes_listed.csv --limit 10
python scripts/ingest/download_ecos.py --names base_rate,usd_krw
```

## 보관 정책

- `raw/` 는 **불변** — 다운로드 후 절대 수정하지 않는다. 재현성 보장.
- `processed/` 는 재생성 가능 — 파이프라인이 raw 에서 다시 만들 수 있어야 한다.
- 둘 다 git 추적 외 (사이즈 + 라이선스 이슈).
- 백업은 별도 (S3/cold storage).

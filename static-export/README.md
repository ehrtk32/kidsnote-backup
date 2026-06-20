# Seoi Kidsnote Static Export

Notion에 백업된 Kidsnote 데이터를 WordPress 없이 바로 정적 사이트로 만듭니다.

생성 결과:

```text
/Users/user/vscode/seoi-kidsnote/static-export/dist
```

## Local Preview

```bash
cd /Users/user/vscode/seoi-kidsnote/static-export
python3 export_static_site.py
cd dist
python3 -m http.server 9500
```

열어볼 주소:

```text
http://127.0.0.1:9500
```

## Deploy To Cloudflare Pages

Wrangler CLI를 사용합니다. Cloudflare Pages의 브라우저 직접 업로드는 파일 1,000개 제한이 있어서 이 프로젝트에는 맞지 않습니다.

```bash
cd /Users/user/vscode/seoi-kidsnote/static-export
npx --yes wrangler pages deploy dist --project-name seoi-kidsnote
```

Cloudflare Pages 한도:

- Wrangler upload: 20,000 files
- Drag and drop upload: 1,000 files
- Single asset maximum: 25 MiB

## Daily Sync

로컬 Mac에서는 매일 새벽 4시 30분에 다음 흐름으로 실행되도록 설정할 수 있습니다.

```text
Notion DB 조회 -> 정적 파일 생성 -> Cloudflare Pages(seoi-kidsnote) 배포
```

수동으로 같은 작업을 실행하려면:

```bash
cd /Users/user/vscode/seoi-kidsnote/static-export
./run_daily_export.sh
```

필요한 비밀값은 `static-export/.env`에 두고, 이 파일은 git에 올리지 않습니다.

## GitHub Actions Daily Sync

로컬 PC를 켜두지 않으려면 GitHub Actions가 매일 실행되게 합니다.

```text
GitHub Actions 04:30 KST -> Notion DB 조회 -> 정적 파일 생성 -> Cloudflare Pages(seoi-kidsnote) 배포
```

워크플로 파일:

```text
.github/workflows/seoi-kidsnote-static.yml
```

GitHub repo의 `Settings -> Secrets and variables -> Actions`에 아래 값을 넣습니다.

Secrets:

- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `CLOUDFLARE_API_TOKEN`

선택 사항:

- `CLOUDFLARE_ACCOUNT_ID`

Cloudflare Account ID는 워크플로에 기본값으로 넣어두었습니다. 나중에 다른 Cloudflare 계정으로 옮길 때만 `CLOUDFLARE_ACCOUNT_ID`를 Secrets 또는 Variables에 추가하세요.

Cloudflare API Token에는 `Account -> Cloudflare Pages -> Edit` 권한이 필요합니다. 현재 기본 Account ID는:

```text
88b28a9c6734bd6185852a2804db5469
```

수동 실행:

```text
GitHub -> Actions -> Seoi Kidsnote static site -> Run workflow
```

테스트만 할 때는 `limit`에 `3`, `deploy`에 `off`를 넣으면 최근 3개만 export하고 Cloudflare에는 배포하지 않습니다.

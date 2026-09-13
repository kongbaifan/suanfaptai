# HVAC Runtime Dashboard

The Vue 3 workbench is served by the existing FastAPI application. Browser requests use the existing `/api/v1` REST contract and `/ws/realtime/{project_id}` WebSocket; the browser never opens the Windows `VarLink` pipe.

## Development

```powershell
npm install
npm run dev
```

Vite proxies `/api`, `/health`, and `/ws` to `http://127.0.0.1:8010`, matching the
backend's documented default port (`uvicorn app.main:app --port 8010`).

## Verification And Production Build

```powershell
npm run test:unit
npm run typecheck
npm run build
```

`npm run build` writes the static entrypoint and assets to `../app/static`, which is mounted by FastAPI at `/static` and used by `/`.

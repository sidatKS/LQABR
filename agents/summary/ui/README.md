# ui — the Summary Agent's front end

The AG-UI client from the original blog-summarizer app, pointed at this
agent's `/chat` endpoint instead of a hard-coded localhost port.

    npm install
    npm run dev                     # talks to http://localhost:8080/chat

    VITE_SUMMARY_CHAT_URL=https://<cloud-run-url>/chat npm run build

`/chat` exists only when the service is deployed with
`LQABR_SUMMARY_ROUTES=all` (the default) and `LQABR_SUMMARY_ENABLE_AGUI=1`,
and the browser origin must be listed in `LQABR_SUMMARY_CORS_ORIGINS`.

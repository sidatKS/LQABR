import { HttpAgent } from "@ag-ui/client";

// The backend URL is build-time configuration, never a literal in the code:
//   VITE_SUMMARY_CHAT_URL=https://<cloud-run-url>/chat npm run build
// Defaults to the local uvicorn port documented in the agent README.
export const CHAT_URL: string =
    import.meta.env.VITE_SUMMARY_CHAT_URL ?? "http://localhost:8080/chat";

export const agent = new HttpAgent({
    url: CHAT_URL,
});

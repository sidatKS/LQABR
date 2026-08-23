import { useMemo, useState } from "react";
import { HttpAgent } from "@ag-ui/client";
import "./App.css";
import { CHAT_URL } from "./agui";

/*
 * ============================================
 * Raw AG-UI event
 * ============================================
 */
interface UIEvent {
    id: number;
    type: string;
    timestamp: string;
    data: any;
}


/*
 * ============================================
 * User-facing workflow step
 * ============================================
 */
interface WorkflowStep {
    id: string;
    title: string;
    icon: string;

    status:
        | "pending"
        | "running"
        | "completed"
        | "error";

    events: UIEvent[];
}


/*
 * ============================================
 * Workflow definitions
 * ============================================
 *
 * These are the business-level steps that
 * the user sees.
 */
const WORKFLOW_DEFINITIONS = [
    {
        id: "agent",
        title: "Agent Started",
        icon: "🤖",
    },

    {
        id: "crawl",
        title: "Crawling Blog",
        icon: "🌐",
    },

    {
        id: "extract",
        title: "Article Extracted",
        icon: "📄",
    },

    {
        id: "analysis",
        title: "Claude Analyzing",
        icon: "🧠",
    },

    {
        id: "summary",
        title: "Generating Summary",
        icon: "📊",
    },

    {
        id: "complete",
        title: "Completed",
        icon: "✅",
    },
];


/*
 * ============================================
 * Main component
 * ============================================
 */
function App() {

    /*
     * Blog URL
     */
    const [url, setUrl] = useState(
        "https://spring.io/blog/"
    );


    /*
     * Claude response
     */
    const [response, setResponse] =
        useState("");


    /*
     * Raw AG-UI events
     */
    const [events, setEvents] =
        useState<UIEvent[]>([]);


    /*
     * Loading state
     */
    const [loading, setLoading] =
        useState(false);


    /*
     * Error
     */
    const [error, setError] =
        useState("");


    /*
     * Create HttpAgent once.
     */
    const [agent] = useState(
        () =>
            new HttpAgent({
                url:
                    CHAT_URL,
            })
    );


    /*
     * ==========================================
     * Add AG-UI event
     * ==========================================
     */
    const addEvent = (
        event: any
    ) => {

        console.log(
            "AG-UI EVENT:",
            event
        );


        const newEvent: UIEvent = {

            id:
                Date.now() +
                Math.random(),

            type:
                event.type ??
                "UNKNOWN",

            timestamp:
                new Date().toLocaleTimeString(),

            data: event,
        };


        setEvents(
            previous => [
                ...previous,
                newEvent,
            ]
        );
    };


    /*
     * ==========================================
     * Start summarization
     * ==========================================
     */
    const summarizeBlog =
        async () => {

            /*
             * Validate URL
             */
            if (!url.trim()) {

                setError(
                    "Please enter a blog URL."
                );

                return;
            }


            /*
             * Reset state
             */
            setLoading(true);

            setResponse("");

            setError("");

            setEvents([]);


            /*
             * Add user message
             */
            agent.messages = [

                {
                    id:
                        crypto.randomUUID(),

                    role: "user",

                    content:
                        `Summarize this blog:

${url}`,
                },

            ];


            /*
             * ======================================
             * Subscribe to AG-UI events
             * ======================================
             *
             * IMPORTANT:
             * Subscribe BEFORE runAgent().
             */
            const subscription =
                agent.subscribe({

                    onEvent:
                        ({ event }) => {

                            /*
                             * Store every raw event.
                             */
                            addEvent(event);


                            /*
                             * Stream Claude response.
                             */
                            if (
                                event.type ===
                                "TEXT_MESSAGE_CONTENT"
                            ) {

                                setResponse(
                                    previous =>
                                        previous +
                                        (event.delta ?? "")
                                );

                            }

                        },

                });


            try {

                /*
                 * Start AG-UI run.
                 */
                const result =
                    await agent.runAgent({

                        runId:
                            crypto.randomUUID(),

                        tools: [],

                        context: [],

                    });


                console.log(
                    "AG-UI RESULT:",
                    result
                );


            } catch (
                runError
                ) {

                console.error(
                    "AG-UI ERROR:",
                    runError
                );


                /*
                 * Add error event.
                 */
                addEvent({

                    type:
                        "RUN_ERROR",

                    message:
                        runError instanceof Error
                            ? runError.message
                            : "Unknown error",

                });


                if (
                    runError instanceof Error
                ) {

                    setError(
                        runError.message
                    );

                } else {

                    setError(
                        "An unknown error occurred."
                    );

                }


            } finally {

                /*
                 * Stop subscription.
                 */
                subscription.unsubscribe();

                setLoading(false);

            }
        };


    /*
     * ==========================================
     * Convert raw AG-UI events into workflow
     * ==========================================
     */
    const workflowSteps =
        useMemo(
            () =>
                buildWorkflowSteps(
                    events,
                    loading
                ),
            [
                events,
                loading,
            ]
        );


    return (

        <div className="app">


            {/* =====================================
          HEADER
      ===================================== */}

            <header className="app-header">

                <div className="header-inner">

                    <div className="brand-icon">
                        🤖
                    </div>


                    <div>

                        <h1>
                            AI Blog Summarizer
                        </h1>

                        <p>
                            Google ADK + Claude + AG-UI
                        </p>

                    </div>

                </div>

            </header>


            {/* =====================================
          MAIN
      ===================================== */}

            <main className="main-container">


                {/* ===================================
            BLOG INPUT
        =================================== */}

                <section className="card input-card">

                    <div className="section-title">

                        <h2>
                            Summarize a Blog
                        </h2>

                        <p>
                            Enter a technical blog URL
                            and let the AI agent crawl,
                            analyze and summarize it.
                        </p>

                    </div>


                    <div className="form-group">

                        <label htmlFor="blog-url">
                            Blog URL
                        </label>


                        <input
                            id="blog-url"

                            type="url"

                            value={url}

                            onChange={
                                event =>
                                    setUrl(
                                        event.target.value
                                    )
                            }

                            placeholder={
                                "https://example.com/blog"
                            }

                            disabled={loading}
                        />

                    </div>


                    <button
                        className="summarize-button"

                        onClick={
                            summarizeBlog
                        }

                        disabled={loading}
                    >

                        {loading ? (

                            <>
                                <span className="spinner" />

                                Analyzing...
                            </>

                        ) : (

                            <>
                                ✨

                                <span>
                  Summarize Blog
                </span>
                            </>

                        )}

                    </button>


                    {error && (

                        <div className="error-message">

              <span>
                ❌
              </span>

                            <span>
                {error}
              </span>

                        </div>

                    )}

                </section>


                {/* ===================================
            AGENT ACTIVITY
        =================================== */}

                {events.length > 0 && (

                    <section className="card pipeline-card">


                        {/* Pipeline header */}

                        <div className="pipeline-header">

                            <div>

                                <h2>
                                    Agent Activity
                                </h2>

                                <p>
                                    Real-time AG-UI execution
                                </p>

                            </div>


                            {loading ? (

                                <span className="live-badge">

                  <span className="live-dot" />

                  LIVE

                </span>

                            ) : (

                                <span className="completed-badge">

                  ✓ COMPLETED

                </span>

                            )}

                        </div>


                        {/* =================================
                HORIZONTAL PIPELINE
            ================================= */}

                        <div className="pipeline-scroll">

                            <div className="workflow">

                                {workflowSteps.map(
                                    (
                                        step,
                                        index
                                    ) => (

                                        <WorkflowStepComponent

                                            key={
                                                step.id
                                            }

                                            step={
                                                step
                                            }

                                            isLast={
                                                index ===
                                                workflowSteps.length -
                                                1
                                            }

                                        />

                                    )
                                )}

                            </div>

                        </div>

                    </section>

                )}


                {/* ===================================
            BLOG SUMMARY
        =================================== */}

                {(loading ||
                    response) && (

                    <section className="card summary-card">


                        {/* Summary header */}

                        <div className="summary-header">

                            <div>

                                <h2>
                                    Blog Summary
                                </h2>

                                <p>
                                    Generated by Claude
                                </p>

                            </div>


                            {!loading &&
                                response && (

                                    <span className="completed-badge">

                  ✓ COMPLETED

                </span>

                                )}

                        </div>


                        {/* Loading */}

                        {loading &&
                            !response && (

                                <div className="loading-area">

                                    <div className="large-spinner" />

                                    <h3>
                                        Agent is working...
                                    </h3>

                                    <p>
                                        Crawling the blog and
                                        analyzing its content.
                                    </p>

                                </div>

                            )}


                        {/* Response */}

                        {response && (

                            <div className="summary-content">

                <pre>
                  {response}
                </pre>

                            </div>

                        )}

                    </section>

                )}

            </main>


            {/* =====================================
          FOOTER
      ===================================== */}

            <footer className="app-footer">

        <span>
          AG-UI
        </span>

                <span>•</span>

                <span>
          Google ADK
        </span>

                <span>•</span>

                <span>
          Claude
        </span>

            </footer>

        </div>
    );
}


/*
 * ============================================
 * Map raw AG-UI event to logical workflow
 * ============================================
 */
function getWorkflowStepId(
    event: UIEvent
): string | null {

    switch (event.type) {

        /*
         * Agent lifecycle
         */
        case "RUN_STARTED":

            return "agent";


        /*
         * Tool execution
         *
         * These three events are grouped
         * into one "Crawling Blog" step.
         */
        case "TOOL_CALL_START":

        case "TOOL_CALL_ARGS":

        case "TOOL_CALL_END":

            return "crawl";


        /*
         * Claude starts processing.
         */
        case "TEXT_MESSAGE_START":

            return "analysis";


        /*
         * Claude generates content.
         *
         * Multiple TEXT_MESSAGE_CONTENT
         * events will be grouped together.
         */
        case "TEXT_MESSAGE_CONTENT":

            return "summary";


        /*
         * Claude finished message.
         */
        case "TEXT_MESSAGE_END":

            return "summary";


        /*
         * Agent completed.
         */
        case "RUN_FINISHED":

            return "complete";


        /*
         * Error.
         */
        case "RUN_ERROR":

            return "complete";


        default:

            return null;
    }
}


/*
 * ============================================
 * Build workflow
 * ============================================
 */
function buildWorkflowSteps(
    events: UIEvent[],
    loading: boolean
): WorkflowStep[] {

    /*
     * Create all workflow steps.
     */
    const steps: WorkflowStep[] =
        WORKFLOW_DEFINITIONS.map(
            definition => ({

                id:
                definition.id,

                title:
                definition.title,

                icon:
                definition.icon,

                status:
                    "pending",

                events: [],

            })
        );


    /*
     * ==========================================
     * Put raw events into their group.
     * ==========================================
     */
    for (
        const event of events
        ) {

        const stepId =
            getWorkflowStepId(
                event
            );


        if (!stepId) {
            continue;
        }


        const step =
            steps.find(
                item =>
                    item.id ===
                    stepId
            );


        if (!step) {
            continue;
        }


        /*
         * Store original event.
         */
        step.events.push(
            event
        );


        /*
         * Error.
         */
        if (
            event.type ===
            "RUN_ERROR"
        ) {

            step.status =
                "error";

        } else {

            step.status =
                "completed";

        }

    }


    /*
     * ==========================================
     * Determine current running step.
     * ==========================================
     */
    if (loading) {

        /*
         * Find the last workflow step
         * that received an event.
         */
        let lastActiveIndex =
            -1;


        steps.forEach(
            (
                step,
                index
            ) => {

                if (
                    step.events.length > 0
                ) {

                    lastActiveIndex =
                        index;

                }

            }
        );


        if (
            lastActiveIndex >= 0
        ) {

            /*
             * Mark last active step
             * as running.
             */
            steps[
                lastActiveIndex
                ].status =
                "running";

        }

    }


    /*
     * ==========================================
     * If run completed, ensure Completed step
     * is completed.
     * ==========================================
     */
    if (!loading) {

        const completeStep =
            steps.find(
                step =>
                    step.id ===
                    "complete"
            );


        if (
            completeStep &&
            completeStep.events.length > 0
        ) {

            completeStep.status =
                "completed";

        }

    }


    return steps;
}


/*
 * ============================================
 * Workflow step component
 * ============================================
 */
function WorkflowStepComponent({
                                   step,
                                   isLast,
                               }: {
    step: WorkflowStep;
    isLast: boolean;
}) {

    return (

        <div className="workflow-step">


            {/* =====================================
          NODE
      ===================================== */}

            <div
                className={
                    `workflow-node ${step.status}`
                }
            >

        <span>
          {step.icon}
        </span>

            </div>


            {/* =====================================
          CONNECTOR
      ===================================== */}

            {!isLast && (

                <div className="workflow-connector">

                    <div className="workflow-line" />

                    <span>
            →
          </span>

                </div>

            )}


            {/* =====================================
          TITLE
      ===================================== */}

            <div className="workflow-content">

                <div className="workflow-title">

                    {step.title}

                </div>


                {/* ===================================
            STATUS
        =================================== */}

                <div
                    className={
                        `workflow-status ${step.status}`
                    }
                >

                    {getStatusLabel(
                        step.status
                    )}

                </div>


                {/* ===================================
            RAW EVENTS
        =================================== */}

                {step.events.length > 0 && (

                    <details className="event-details">

                        <summary>

                            {step.events.length}

                            {" "}

                            AG-UI event
                            {step.events.length !== 1
                                ? "s"
                                : ""}

                        </summary>


                        <div className="raw-events">

                            {step.events.map(
                                event => (

                                    <div
                                        className="raw-event"
                                        key={event.id}
                                    >

                                        <div className="raw-event-type">

                                            {event.type}

                                        </div>


                                        <pre>

                      {JSON.stringify(
                          event.data,
                          null,
                          2
                      )}

                    </pre>

                                    </div>

                                )
                            )}

                        </div>

                    </details>

                )}

            </div>

        </div>
    );
}


/*
 * ============================================
 * Status label
 * ============================================
 */
function getStatusLabel(
    status:
        | "pending"
        | "running"
        | "completed"
        | "error"
): string {

    switch (status) {

        case "pending":
            return "Waiting";

        case "running":
            return "Running...";

        case "completed":
            return "Completed";

        case "error":
            return "Failed";

        default:
            return "";
    }
}


/*
 * ============================================
 * Export
 * ============================================
 */
export default App;
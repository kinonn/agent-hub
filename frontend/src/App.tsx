import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";

type User = { id: string; display_name: string; email?: string; role: string; is_active?: boolean };
type Project = { id: string; slug: string; name: string; access_role?: string };
type Agent = { id: string; name: string; agent_type: string; status: string; capabilities: string[]; last_seen_at?: string | null; role?: string };
type Task = { id: string; title: string; task_type: string; status: string; priority: number; project: string; lease_owner_agent_id?: string | null; lease_expires_at?: string | null; attempt_count: number; max_attempts?: number; created_at: string; updated_at?: string | null; result_summary?: string | null };
type Message = { id: string; subject: string; body: string; message_type: string; sender_agent_id?: string | null; created_at: string };
type Artifact = { id: string; title: string; summary?: string | null; artifact_type: string; publication_status: string; version: number; file_path: string; pull_request_url?: string | null; created_at: string; commit_sha?: string | null };

const API = "/api/v1";
const NAV = [
  ["overview", "Overview", "◈"], ["agents", "Agents", "◎"], ["tasks", "Tasks", "▤"],
  ["messages", "Messages", "✉"], ["knowledge", "Knowledge", "⌕"], ["artifacts", "Artifacts", "◇"],
  ["settings", "Settings", "⚙"],
] as const;
type Page = (typeof NAV)[number][0];

async function request<T>(path: string, options: RequestInit = {}, csrf?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (csrf && options.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) headers.set("X-CSRF-Token", csrf);
  const response = await fetch(`${API}${path}`, { ...options, headers, credentials: "include" });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.error?.message || `Request failed (${response.status})`);
  return body as T;
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function statusClass(status: string) {
  return `status status-${status.replaceAll("_", "-")}`;
}

function agentOnline(agent: Agent) {
  if (agent.status !== "active" || !agent.last_seen_at) return false;
  const lastSeen = new Date(agent.last_seen_at).valueOf();
  return Number.isFinite(lastSeen) && Date.now() - lastSeen < 90_000;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [csrf, setCsrf] = useState("");
  const [page, setPage] = useState<Page>("overview");
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [search, setSearch] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [health, setHealth] = useState<any>(null);
  const [audit, setAudit] = useState<any[]>([]);
  const [outboxFailures, setOutboxFailures] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [tokenOnce, setTokenOnce] = useState("");
  const eventCursor = useRef(0);

  const refresh = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    setError("");
    try {
      const p = await request<{ projects: Project[] }>("/projects");
      setProjects(p.projects);
      const activeProject = p.projects.find((item) => item.slug === project) ?? p.projects[0];
      setProject(activeProject?.slug ?? "");
      if (!activeProject) return;
      const [a, t, m, art] = await Promise.all([
        request<{ agents: Agent[] }>(`/projects/${activeProject.slug}/agents`),
        request<{ tasks: Task[] }>(`/projects/${activeProject.slug}/tasks?claimable_only=false&limit=100`),
        request<{ messages: Message[] }>(`/projects/${activeProject.slug}/messages?limit=50`),
        request<{ artifacts: Artifact[] }>(`/projects/${activeProject.slug}/artifacts?limit=50`),
      ]);
      setAgents(a.agents);
      setTasks(t.tasks);
      setMessages(m.messages);
      setArtifacts(art.artifacts);
      if (user.role === "admin") {
        const [h, logs, outbox] = await Promise.all([
          request<any>("/health/details"),
          request<{ audit: any[] }>("/audit?limit=12"),
          request<{ events: any[] }>("/events/outbox?limit=10"),
        ]);
        setHealth(h);
        setAudit(logs.audit);
        setOutboxFailures(outbox.events);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to refresh dashboard.");
    } finally {
      setLoading(false);
    }
  }, [user, project]);

  useEffect(() => {
    let alive = true;
    request<{ type: string; id: string; display_name: string; role: string; csrf_token?: string }>("/auth/me")
      .then((result) => {
        if (!alive) return;
        setUser({ id: result.id, display_name: result.display_name, role: result.role });
        setCsrf(result.csrf_token || "");
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!user) return;
    const refreshTimer = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(refreshTimer);
  }, [user, refresh]);

  useEffect(() => {
    if (!user) return;
    let fallback: number | undefined;
    let debounce: number | undefined;
    const source = new EventSource(`${API}/events/stream?after_seq=${eventCursor.current}`, { withCredentials: true });
    const onEvent = (event: Event) => {
      const seq = Number((event as MessageEvent).lastEventId);
      if (Number.isFinite(seq)) eventCursor.current = Math.max(eventCursor.current, seq);
      window.clearTimeout(debounce);
      debounce = window.setTimeout(() => void refresh(), 250);
    };
    const poll = async () => {
      try {
        const result = await request<{ events: unknown[]; next_seq: number; resync_required: boolean }>(
          `/events?after_seq=${eventCursor.current}&limit=100`,
        );
        eventCursor.current = Math.max(eventCursor.current, result.next_seq);
        if (result.events.length || result.resync_required) await refresh();
      } catch {
        // The next polling iteration will retry; the API error is shown by the normal refresh.
      }
    };
    source.onmessage = onEvent;
    for (const eventName of [
      "task.created", "task.available", "task.claimed", "task.updated", "task.completed",
      "task.failed", "task.cancelled", "message.created", "artifact.created", "artifact.in_review",
      "artifact.published", "artifact.rejected", "artifact.failed", "artifact.superseded", "resync",
    ]) source.addEventListener(eventName, onEvent);
    source.onerror = () => {
      source.close();
      if (!fallback) {
        void poll();
        fallback = window.setInterval(() => void poll(), 10000);
      }
    };
    return () => { source.close(); window.clearTimeout(debounce); if (fallback) window.clearInterval(fallback); };
  }, [user, refresh]);

  const visibleNav = useMemo(() => NAV.filter(([key]) => key !== "settings" || user?.role === "admin"), [user]);

  async function login(email: string, password: string) {
    const result = await request<{ user: User; csrf_token: string }>("/auth/login", {
      method: "POST", body: JSON.stringify({ email, password }),
    });
    setUser(result.user);
    setCsrf(result.csrf_token);
  }

  async function logout() {
    try { await request("/auth/logout", { method: "POST" }, csrf); } catch { /* cookie may already have expired */ }
    setUser(null); setCsrf(""); setProject("");
  }

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    if (!project || !search.trim()) return;
    setBusy(true); setError("");
    try {
      const result = await request<{ results: any[] }>(`/projects/${project}/search?q=${encodeURIComponent(search)}&limit=20`);
      setSearchResults(result.results);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Search failed."); }
    finally { setBusy(false); }
  }

  async function cancelTask(taskId: string) {
    try { await request(`/tasks/${taskId}/cancel`, { method: "POST" }, csrf); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not cancel task."); }
  }

  async function retryTask(taskId: string) {
    try { await request(`/tasks/${taskId}/retry`, { method: "POST" }, csrf); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not retry task."); }
  }

  if (!user) return <Login onLogin={login} />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">A</span><span>Agent<span className="brand-light">Hub</span><small>COORDINATION LAYER</small></span></div>
        <div className="workspace-label">WORKSPACE</div>
        <div className="project-picker">
          <span className="project-dot" />
          <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Project">
            {projects.map((item) => <option key={item.id} value={item.slug}>{item.name}</option>)}
          </select>
          <span className="chevron">⌄</span>
        </div>
        <nav className="nav-list">
          {visibleNav.map(([key, label, icon]) => (
            <button key={key} className={`nav-item ${page === key ? "active" : ""}`} onClick={() => setPage(key)}>
              <span className="nav-icon">{icon}</span>{label}
              {key === "tasks" && tasks.filter((task) => task.status === "pending").length > 0 && <span className="nav-count">{tasks.filter((task) => task.status === "pending").length}</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection-status"><span className="live-dot" /> All systems operational</div>
          <div className="profile-card"><div className="avatar">{user.display_name.slice(0, 1).toUpperCase()}</div><div className="profile-copy"><strong>{user.display_name}</strong><span>{user.role}</span></div><button className="icon-button" title="Sign out" onClick={logout}>↗</button></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>AgentHub</span><b>/</b><strong>{visibleNav.find(([key]) => key === page)?.[1]}</strong></div>
          <div className="topbar-actions"><span className="live-pill"><i /> LIVE</span><button className="icon-button refresh-button" title="Refresh" onClick={() => void refresh()} disabled={loading}>⟳</button><div className="top-avatar">{user.display_name.slice(0, 1).toUpperCase()}</div></div>
        </header>
        <section className="content">
          {error && <div className="alert"><span>!</span>{error}<button onClick={() => setError("")}>×</button></div>}
          {!projects.length ? <EmptyState title="No projects yet" body="An administrator needs to add your account to a project." /> : <>
            {page === "overview" && <Overview project={projects.find((item) => item.slug === project)} agents={agents} tasks={tasks} artifacts={artifacts} messages={messages} health={health} audit={audit} outboxFailures={outboxFailures} onNavigate={setPage} />}
            {page === "agents" && <Agents agents={agents} />}
            {page === "tasks" && <Tasks tasks={tasks} canManage={user.role === "admin" || projects.find((p) => p.slug === project)?.access_role === "maintainer"} onCancel={cancelTask} onRetry={retryTask} />}
            {page === "messages" && <Messages messages={messages} />}
            {page === "knowledge" && <Knowledge query={search} setQuery={setSearch} results={searchResults} artifacts={artifacts} busy={busy} onSubmit={runSearch} />}
            {page === "artifacts" && <Artifacts artifacts={artifacts} />}
            {page === "settings" && user.role === "admin" && <Settings csrf={csrf} onToken={setTokenOnce} onRefresh={refresh} projects={projects} currentProject={project} />}
          </>}
          <footer className="footer"><span>AGENTHUB <b>•</b> SELF-HOSTED</span><span>{loading ? "Syncing…" : "Synced " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></footer>
        </section>
      </main>
      {tokenOnce && <div className="modal-backdrop"><div className="modal"><button className="modal-close" onClick={() => setTokenOnce("")}>×</button><div className="modal-icon">⚿</div><h2>Save this agent token</h2><p>This token is shown once. Copy it now and store it in your agent's secret configuration.</p><pre>{tokenOnce}</pre><button className="primary-button" onClick={() => { void navigator.clipboard.writeText(tokenOnce); setTokenOnce(""); }}>Copy token</button></div></div>}
    </div>
  );
}

function Login({ onLogin }: { onLogin: (email: string, password: string) => Promise<void> }) {
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(""); try { await onLogin(email, password); } catch (cause) { setError(cause instanceof Error ? cause.message : "Sign in failed."); } finally { setBusy(false); } }
  return <div className="login-screen"><div className="login-card"><div className="brand login-brand"><span className="brand-mark">A</span><span>Agent<span className="brand-light">Hub</span><small>COORDINATION LAYER</small></span></div><div className="eyebrow">PRIVATE WORKSPACE</div><h1>Welcome back</h1><p>Sign in to your AgentHub dashboard.</p>{error && <div className="alert">{error}</div>}<form onSubmit={submit}><label>Email address<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label><label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label><button className="primary-button full-button" disabled={busy}>{busy ? "Signing in…" : "Sign in"}<span>→</span></button></form><div className="login-note"><span className="lock">▣</span> Private, self-hosted agent coordination</div></div></div>;
}

function PageHeading({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{subtitle}</p></div>{action}</div>;
}

function Overview({ project, agents, tasks, artifacts, messages, health, audit, outboxFailures, onNavigate }: any) {
  const activeTasks = tasks.filter((t: Task) => ["claimed", "in_progress"].includes(t.status)).length;
  const needsAttention = tasks.filter((t: Task) => ["failed", "blocked"].includes(t.status));
  return <>
    <PageHeading eyebrow="WORKSPACE OVERVIEW" title={`Good to see you${project ? `, ${project.name}` : ""}.`} subtitle="Here’s what your agents have been working on." action={<button className="secondary-button" onClick={() => onNavigate("tasks")}>View task queue <span>→</span></button>} />
    <div className="metric-grid">
      <Metric label="Active agents" value={agents.length} meta="project members" icon="◎" tint="purple" />
      <Metric label="Pending tasks" value={tasks.filter((t: Task) => t.status === "pending").length} meta="waiting to be claimed" icon="▤" tint="blue" />
      <Metric label="In progress" value={activeTasks} meta="currently being handled" icon="◷" tint="green" />
      <Metric label="Published knowledge" value={artifacts.filter((a: Artifact) => a.publication_status === "published").length} meta="verified artifacts" icon="◇" tint="orange" />
    </div>
    <div className="overview-grid">
      <div className="panel task-panel"><div className="panel-head"><div><h2>Task queue</h2><p>Recent work across your project</p></div><button className="text-button" onClick={() => onNavigate("tasks")}>View all →</button></div><TaskTable tasks={tasks.slice(0, 5)} compact /></div>
      <div className="panel agent-panel"><div className="panel-head"><div><h2>Agent status</h2><p>Project collaborators</p></div><button className="text-button" onClick={() => onNavigate("agents")}>View all →</button></div>{agents.slice(0, 5).map((agent: Agent) => <div className="agent-row" key={agent.id}><div className="agent-avatar">{agent.name.slice(0, 1).toUpperCase()}</div><div className="agent-copy"><strong>{agent.name}</strong><span>{agent.agent_type} · {agent.capabilities?.slice(0, 2).join(", ") || "No capabilities"}</span></div><span className={`presence ${agentOnline(agent) ? "online" : "offline"}`} title={formatDate(agent.last_seen_at)} /></div>)}</div>
      <div className="panel activity-panel"><div className="panel-head"><div><h2>Recent activity</h2><p>Latest project updates and audit events</p></div></div>{messages.slice(-3).reverse().map((message: Message) => <div className="activity-row" key={message.id}><span className="activity-mark">✉</span><div><strong>{message.subject}</strong><span>Message · {formatDate(message.created_at)}</span></div></div>)}{audit.slice(0, 2).map((item: any) => <div className="activity-row" key={item.id}><span className="activity-mark">⌁</span><div><strong>{item.action.replaceAll("_", " ")}</strong><span>Security audit · {formatDate(item.created_at)}</span></div></div>)}{!messages.length && !audit.length && <div className="empty-inline">No recent activity yet.</div>}</div>
      <div className="panel health-panel"><div className="panel-head"><div><h2>System health</h2><p>AgentHub services</p></div><span className="health-indicator"><i /> HEALTHY</span></div><HealthLine label="API & database" value="Operational" /><HealthLine label="Worker heartbeat" value={health?.worker?.heartbeat_age_seconds == null ? "Waiting" : `${health.worker.heartbeat_age_seconds}s ago`} /><HealthLine label="Outbox failures" value={health?.outbox?.failed ?? "—"} /><HealthLine label="SQLite WAL" value={health?.database?.wal_size_bytes == null ? "—" : `${Math.round(health.database.wal_size_bytes / 1024)} KB`} /></div>
      {!!outboxFailures?.length && <div className="panel outbox-panel"><div className="panel-head"><div><h2>Failed notifications</h2><p>Wake-up hints need attention</p></div><span className="status status-failed">{outboxFailures.length} failed</span></div>{outboxFailures.slice(0, 5).map((event: any) => <div className="health-line" key={event.id}><span>{event.event_type} · {event.aggregate_id.slice(0, 8)}<small>{event.last_error || "Delivery failed"}</small></span><strong>{event.attempt_count} attempts</strong></div>)}</div>}
    </div>
    {needsAttention.length > 0 && <div className="attention-banner"><span>!</span><div><strong>{needsAttention.length} task{needsAttention.length > 1 ? "s" : ""} need attention</strong><p>There are blocked or failed items in this project's queue.</p></div><button className="text-button" onClick={() => onNavigate("tasks")}>Review →</button></div>}
  </>;
}

function Metric({ label, value, meta, icon, tint }: any) { return <div className="metric-card"><div className="metric-top"><span>{label}</span><span className={`metric-icon ${tint}`}>{icon}</span></div><div className="metric-value">{value}</div><div className="metric-meta">{meta}</div></div>; }
function HealthLine({ label, value }: any) { return <div className="health-line"><span>{label}</span><strong>{value}</strong></div>; }

function Agents({ agents }: { agents: Agent[] }) {
  return <><PageHeading eyebrow="PROJECT TEAM" title="Agents" subtitle="The AI collaborators with access to this project." /><div className="agent-card-grid">{agents.map((agent) => <div className="panel agent-card" key={agent.id}><div className="agent-card-top"><div className="agent-avatar large">{agent.name.slice(0, 1).toUpperCase()}</div><span className={statusClass(agent.status)}>{agent.status}</span></div><h3>{agent.name}</h3><p>{agent.agent_type} agent <span>·</span> {agent.role || "member"}</p><div className="capability-list">{agent.capabilities?.length ? agent.capabilities.map((cap) => <span key={cap} className="chip">{cap}</span>) : <span className="muted">No capabilities registered</span>}</div><div className="agent-card-foot"><span className={`presence ${agentOnline(agent) ? "online" : "offline"}`} />Last seen {formatDate(agent.last_seen_at)}</div></div>)}</div>{!agents.length && <EmptyState title="No agents yet" body="Agents appear here when they're provisioned and added to this project." />}</>;
}

function Tasks({ tasks, canManage, onCancel, onRetry }: { tasks: Task[]; canManage: boolean; onCancel: (id: string) => void; onRetry: (id: string) => void }) {
  const [filter, setFilter] = useState("all");
  const filtered = tasks.filter((task) => filter === "all" || task.status === filter);
  return <><PageHeading eyebrow="EXECUTION" title="Tasks" subtitle="Track delegated work, ownership, and retries." action={<div className="filter-select"><select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">All statuses</option>{["pending", "claimed", "in_progress", "blocked", "completed", "failed", "cancelled"].map((x) => <option key={x} value={x}>{x.replaceAll("_", " ")}</option>)}</select></div>} /><div className="panel table-panel"><TaskTable tasks={filtered} canManage={canManage} onCancel={onCancel} onRetry={onRetry} /></div>{!filtered.length && <EmptyState title="No tasks to show" body="Tasks created by project agents will appear here." />}</>;
}

function TaskTable({ tasks, compact = false, canManage = false, onCancel, onRetry }: { tasks: Task[]; compact?: boolean; canManage?: boolean; onCancel?: (id: string) => void; onRetry?: (id: string) => void }) {
  return <div className="table-wrap"><table><thead><tr><th>TASK</th><th>STATUS</th><th>OWNER</th><th>ATTEMPTS</th><th>UPDATED</th>{!compact && canManage && <th />}</tr></thead><tbody>{tasks.map((task) => <tr key={task.id}><td><div className="task-title"><strong>{task.title}</strong><span>{task.task_type} · {task.id.slice(0, 8)}</span></div></td><td><span className={statusClass(task.status)}>{task.status.replaceAll("_", " ")}</span></td><td>{task.lease_owner_agent_id ? <div className="owner-cell"><span className="owner-dot" />{task.lease_owner_agent_id.slice(0, 8)}<small>until {formatDate(task.lease_expires_at)}</small></div> : <span className="muted">Unassigned</span>}</td><td>{task.attempt_count}{task.max_attempts ? <span className="muted"> / {task.max_attempts}</span> : null}</td><td>{formatDate(task.updated_at || task.created_at)}</td>{!compact && canManage && <td className="table-actions">{["pending", "claimed", "in_progress", "blocked"].includes(task.status) && <button className="small-action" onClick={() => onCancel?.(task.id)}>Cancel</button>}{task.status === "failed" && <button className="small-action" onClick={() => onRetry?.(task.id)}>Retry</button>}</td>}</tr>)}</tbody></table>{compact && !tasks.length && <div className="empty-inline">No tasks have been created yet.</div>}</div>;
}

function Messages({ messages }: { messages: Message[] }) {
  return <><PageHeading eyebrow="TEAM COMMUNICATION" title="Messages" subtitle="A shared history of agent handoffs and project updates." /><div className="message-list">{messages.map((message) => <article className="panel message-card" key={message.id}><div className="message-meta"><span className="message-type">{message.message_type}</span><span>{formatDate(message.created_at)}</span></div><h3>{message.subject}</h3><p>{message.body}</p><div className="message-author">From agent <code>{message.sender_agent_id?.slice(0, 8) || "system"}</code></div></article>)}</div>{!messages.length && <EmptyState title="No messages yet" body="Messages published by your project agents will show up here." />}</>;
}

function Knowledge({ query, setQuery, results, artifacts, busy, onSubmit }: any) {
  return <><PageHeading eyebrow="PROJECT MEMORY" title="Knowledge" subtitle="Search durable research, specifications, and decisions." /><form className="search-box" onSubmit={onSubmit}><span>⌕</span><input placeholder="Search published knowledge…" value={query} onChange={(e) => setQuery(e.target.value)} /><button className="primary-button" disabled={busy}>{busy ? "Searching…" : "Search"}</button></form>{results.length > 0 ? <div className="knowledge-results">{results.map((item: any) => <article className="panel knowledge-card" key={item.artifact_id}><div className="knowledge-top"><span className="chip">{item.artifact_type}</span><span>v{item.version}</span></div><h3>{item.title}</h3><p>{String(item.snippet).replace(/<\/?mark>/g, "")}</p><div className="knowledge-foot"><code>{item.file_path}</code><span>{item.commit_sha?.slice(0, 8) || "Unpublished"}</span></div></article>)}</div> : <div className="panel knowledge-intro"><div className="knowledge-orb">⌕</div><h2>Knowledge that compounds.</h2><p>Search across published project artifacts. Every result links back to its source in the private knowledge repository.</p><div className="suggestions"><span>Try searching for</span>{["architecture", "research", "decision"].map((x) => <button key={x} onClick={() => setQuery(x)}>{x}</button>)}</div></div>}<div className="section-heading"><h2>Recently published</h2><span>{artifacts.filter((a: Artifact) => a.publication_status === "published").length} artifacts</span></div><ArtifactsTable artifacts={artifacts.filter((a: Artifact) => a.publication_status === "published").slice(0, 5)} /></>;
}

function Artifacts({ artifacts }: { artifacts: Artifact[] }) {
  return <><PageHeading eyebrow="GITHUB PUBLICATION" title="Artifacts" subtitle="Review staged knowledge and track pull-request publication." /><div className="artifact-stats">{["draft", "in_review", "published", "rejected", "failed"].map((status) => <div className="artifact-stat" key={status}><span className={statusClass(status)}>{status.replaceAll("_", " ")}</span><strong>{artifacts.filter((item) => item.publication_status === status).length}</strong></div>)}</div><div className="panel table-panel"><ArtifactsTable artifacts={artifacts} /></div></>;
}

function ArtifactsTable({ artifacts }: { artifacts: Artifact[] }) {
  return <div className="table-wrap"><table><thead><tr><th>ARTIFACT</th><th>TYPE</th><th>VERSION</th><th>STATUS</th><th>CREATED</th><th>SOURCE</th></tr></thead><tbody>{artifacts.map((item) => <tr key={item.id}><td><div className="task-title"><strong>{item.title}</strong><span>{item.summary || item.file_path}</span></div></td><td>{item.artifact_type}</td><td>v{item.version}</td><td><span className={statusClass(item.publication_status)}>{item.publication_status.replaceAll("_", " ")}</span></td><td>{formatDate(item.created_at)}</td><td>{item.pull_request_url ? <a href={item.pull_request_url} target="_blank" rel="noreferrer">Open PR ↗</a> : <span className="muted">{item.commit_sha?.slice(0, 8) || "—"}</span>}</td></tr>)}</tbody></table>{!artifacts.length && <div className="empty-inline">No artifacts in this project yet.</div>}</div>;
}

function Settings({ csrf, onToken, onRefresh, projects, currentProject }: {
  csrf: string;
  onToken: (token: string) => void;
  onRefresh: () => Promise<void>;
  projects: Project[];
  currentProject: string;
}) {
  const [agentName, setAgentName] = useState("");
  const [agentType, setAgentType] = useState("hermes");
  const [projectName, setProjectName] = useState("");
  const [projectSlug, setProjectSlug] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [userName, setUserName] = useState("");
  const [userPassword, setUserPassword] = useState("");
  const [adminAgents, setAdminAgents] = useState<Agent[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [membershipProject, setMembershipProject] = useState(currentProject);
  const [memberAgent, setMemberAgent] = useState("");
  const [agentRole, setAgentRole] = useState("contributor");
  const [memberUser, setMemberUser] = useState("");
  const [userRole, setUserRole] = useState("viewer");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const loadAdminData = useCallback(async () => {
    try {
      const [agentResult, userResult] = await Promise.all([
        request<{ agents: Agent[] }>("/agents"),
        request<{ users: User[] }>("/users"),
      ]);
      setAdminAgents(agentResult.agents);
      setUsers(userResult.users);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "Could not load administration data.");
    }
  }, []);

  useEffect(() => { void loadAdminData(); }, [loadAdminData]);
  useEffect(() => {
    if (projects.some((item) => item.slug === currentProject)) setMembershipProject(currentProject);
  }, [projects, currentProject]);

  async function mutate(action: () => Promise<void>, success: string) {
    setBusy(true);
    setNotice("");
    try {
      await action();
      setNotice(success);
      await loadAdminData();
      await onRefresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "The administrative change failed.");
    } finally {
      setBusy(false);
    }
  }

  async function addAgent(event: FormEvent) {
    event.preventDefault();
    await mutate(async () => {
      const result = await request<{ token: string }>("/agents", {
        method: "POST", body: JSON.stringify({ name: agentName, agent_type: agentType }),
      }, csrf);
      onToken(result.token);
      setAgentName("");
    }, "Agent created.");
  }

  async function addProject(event: FormEvent) {
    event.preventDefault();
    await mutate(async () => {
      await request("/projects", {
        method: "POST", body: JSON.stringify({ name: projectName, slug: projectSlug }),
      }, csrf);
      setProjectName("");
      setProjectSlug("");
    }, "Project created.");
  }

  async function addUser(event: FormEvent) {
    event.preventDefault();
    await mutate(async () => {
      await request("/users", {
        method: "POST", body: JSON.stringify({ email: userEmail, display_name: userName, password: userPassword }),
      }, csrf);
      setUserEmail("");
      setUserName("");
      setUserPassword("");
    }, "Dashboard user created.");
  }

  async function addAgentMembership(event: FormEvent) {
    event.preventDefault();
    await mutate(async () => {
      await request(`/projects/${membershipProject}/members/${memberAgent}`, {
        method: "PUT", body: JSON.stringify({ role: agentRole }),
      }, csrf);
    }, "Agent project access updated.");
  }

  async function addUserMembership(event: FormEvent) {
    event.preventDefault();
    await mutate(async () => {
      await request(`/projects/${membershipProject}/users/${memberUser}`, {
        method: "PUT", body: JSON.stringify({ role: userRole }),
      }, csrf);
    }, "Dashboard user project access updated.");
  }

  async function rotateToken(agentId: string) {
    await mutate(async () => {
      const result = await request<{ token: string }>(`/agents/${agentId}/tokens`, { method: "POST" }, csrf);
      onToken(result.token);
    }, "Agent token rotated.");
  }

  async function toggleAgent(agent: Agent) {
    const status = agent.status === "active" ? "disabled" : "active";
    await mutate(async () => {
      await request(`/agents/${agent.id}`, { method: "PATCH", body: JSON.stringify({ status }) }, csrf);
      if (status === "active") {
        const result = await request<{ token: string }>(`/agents/${agent.id}/tokens`, { method: "POST" }, csrf);
        onToken(result.token);
      }
    }, `Agent ${status}.`);
  }

  async function toggleUser(user: User, active: boolean) {
    await mutate(async () => {
      await request(`/users/${user.id}`, {
        method: "PATCH", body: JSON.stringify({ is_active: active }),
      }, csrf);
    }, `Dashboard user ${active ? "activated" : "deactivated"}.`);
  }

  return <>
    <PageHeading eyebrow="ADMINISTRATION" title="Settings" subtitle="Manage users, agents, projects, and project access." />
    <div className="settings-grid">
      <div className="panel settings-card">
        <div className="settings-icon">◎</div><h2>Provision an agent</h2>
        <p>Create an agent identity and issue a token. The token is displayed once.</p>
        <form onSubmit={addAgent}>
          <label>Agent name<input value={agentName} onChange={(e) => setAgentName(e.target.value)} placeholder="Hermes Researcher" required /></label>
          <label>Agent type<select value={agentType} onChange={(e) => setAgentType(e.target.value)}><option value="hermes">Hermes</option><option value="opencode">OpenCode</option><option value="copilot">Copilot</option><option value="other">Other</option></select></label>
          <button className="primary-button" disabled={busy}>Create agent <span>→</span></button>
        </form>
      </div>
      <div className="panel settings-card">
        <div className="settings-icon blue">▦</div><h2>Create a project</h2>
        <p>Projects define an isolated agent workspace and knowledge namespace.</p>
        <form onSubmit={addProject}>
          <label>Project name<input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="Product research" required /></label>
          <label>Project slug<input value={projectSlug} onChange={(e) => setProjectSlug(e.target.value)} placeholder="product-research" pattern="[a-z0-9][a-z0-9-]{1,62}" required /></label>
          <button className="primary-button" disabled={busy}>Create project <span>→</span></button>
        </form>
      </div>
      <div className="panel settings-card">
        <div className="settings-icon blue">⌂</div><h2>Create a dashboard user</h2>
        <p>Human accounts use Argon2id passwords and project-scoped access.</p>
        <form onSubmit={addUser}>
          <label>Display name<input value={userName} onChange={(e) => setUserName(e.target.value)} required /></label>
          <label>Email<input type="email" value={userEmail} onChange={(e) => setUserEmail(e.target.value)} required /></label>
          <label>Initial password<input type="password" minLength={12} value={userPassword} onChange={(e) => setUserPassword(e.target.value)} required /></label>
          <button className="primary-button" disabled={busy}>Create user <span>→</span></button>
        </form>
      </div>
      <div className="panel settings-card">
        <div className="settings-icon">⌘</div><h2>Project membership</h2>
        <p>Grant agents and dashboard users the least privilege they need.</p>
        <label>Project<select value={membershipProject} onChange={(e) => setMembershipProject(e.target.value)}>{projects.map((item) => <option key={item.id} value={item.slug}>{item.name}</option>)}</select></label>
        <form onSubmit={addAgentMembership}>
          <label>Agent<select value={memberAgent} onChange={(e) => setMemberAgent(e.target.value)} required><option value="">Select an agent</option>{adminAgents.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.status})</option>)}</select></label>
          <label>Agent role<select value={agentRole} onChange={(e) => setAgentRole(e.target.value)}><option value="reader">Reader</option><option value="contributor">Contributor</option><option value="maintainer">Maintainer</option></select></label>
          <button className="secondary-button" disabled={busy || !membershipProject}>Save agent access</button>
        </form>
        <form onSubmit={addUserMembership}>
          <label>Dashboard user<select value={memberUser} onChange={(e) => setMemberUser(e.target.value)} required><option value="">Select a user</option>{users.map((item) => <option key={item.id} value={item.id}>{item.display_name} ({item.email})</option>)}</select></label>
          <label>User role<select value={userRole} onChange={(e) => setUserRole(e.target.value)}><option value="viewer">Viewer</option><option value="maintainer">Maintainer</option></select></label>
          <button className="secondary-button" disabled={busy || !membershipProject}>Save user access</button>
        </form>
      </div>
    </div>
    {notice && <div className="notice">{notice}</div>}
    <div className="panel table-panel admin-agent-table"><div className="panel-head"><div><h2>Dashboard users</h2><p>Deactivate access or change account roles.</p></div></div><div className="table-wrap"><table><thead><tr><th>USER</th><th>EMAIL</th><th>ROLE</th><th>STATUS</th><th>ACTIONS</th></tr></thead><tbody>{users.map((user) => <tr key={user.id}><td>{user.display_name}</td><td>{user.email}</td><td><select aria-label={`Role for ${user.email}`} value={user.role} onChange={(e) => { void mutate(async () => { await request(`/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ role: e.target.value }) }, csrf); }, "Dashboard user role updated."); }}><option value="member">Member</option><option value="admin">Admin</option></select></td><td><span className={statusClass(user.is_active === false ? "disabled" : "active")}>{user.is_active === false ? "disabled" : "active"}</span></td><td><button className="small-action" onClick={() => void toggleUser(user, user.is_active === false)}>{user.is_active === false ? "Activate" : "Deactivate"}</button></td></tr>)}</tbody></table></div></div>
    <div className="panel table-panel admin-agent-table"><div className="panel-head"><div><h2>Registered agents</h2><p>Rotate tokens or disable access immediately.</p></div></div><div className="table-wrap"><table><thead><tr><th>AGENT</th><th>TYPE</th><th>STATUS</th><th>CAPABILITIES</th><th>ACTIONS</th></tr></thead><tbody>{adminAgents.map((agent) => <tr key={agent.id}><td><strong>{agent.name}</strong><div className="muted">{agent.id.slice(0, 8)}</div></td><td>{agent.agent_type}</td><td><span className={statusClass(agent.status)}>{agent.status}</span></td><td>{agent.capabilities?.join(", ") || "—"}</td><td className="table-actions"><button className="small-action" onClick={() => void rotateToken(agent.id)}>Rotate token</button><button className="small-action" onClick={() => void toggleAgent(agent)}>{agent.status === "active" ? "Disable" : "Enable"}</button></td></tr>)}</tbody></table></div></div>
    <div className="panel security-note"><span>▣</span><div><strong>Security reminder</strong><p>Agent tokens are high-entropy credentials. Store them in a secret manager; never commit them to source control or paste them into task content.</p></div></div>
  </>;
}

function EmptyState({ title, body }: { title: string; body: string }) { return <div className="empty-state"><div>◌</div><h2>{title}</h2><p>{body}</p></div>; }

create table tickets (
    id text primary key,
    title text not null,
    body text not null default '',
    source text not null,
    source_ref text unique,
    project text,
    labels jsonb not null default '[]',
    department text,
    owner_role text,
    status text not null,
    risk text,
    budget_usd double precision not null,
    spent_usd double precision not null default 0,
    spent_tokens bigint not null default 0,
    parent_id text references tickets (id),
    artifacts jsonb not null default '[]',
    trace_id text,
    lease_owner text,
    lease_until timestamptz,
    pending_resume jsonb,
    approval_deadline timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index tickets_status_idx on tickets (status);

create table ticket_events (
    id bigserial primary key,
    ticket_id text not null references tickets (id),
    kind text not null,
    from_status text,
    to_status text,
    actor text not null,
    data jsonb not null default '{}',
    at timestamptz not null default now()
);

create index ticket_events_ticket_idx on ticket_events (ticket_id, id);

create table approvals (
    id text primary key,
    ticket_id text not null references tickets (id),
    kind text not null,
    round integer not null,
    decision text not null,
    actor text not null,
    comment text not null default '',
    at timestamptz not null default now(),
    unique (ticket_id, kind, round)
);

create table budget_reservations (
    id text primary key,
    ticket_id text not null references tickets (id),
    role text not null,
    model text not null,
    est_usd double precision not null,
    est_tokens bigint not null,
    actual_usd double precision,
    actual_tokens bigint,
    status text not null,
    created_at timestamptz not null default now(),
    settled_at timestamptz
);

create index budget_reservations_ticket_idx on budget_reservations (ticket_id, role);
create index budget_reservations_role_month_idx on budget_reservations (role, created_at);

create table audit_log (
    id bigserial primary key,
    at timestamptz not null default now(),
    ticket_id text,
    role text not null,
    server text not null,
    tool text not null,
    level text,
    decision text not null,
    args jsonb not null,
    result_summary text,
    error text
);

create index audit_log_ticket_idx on audit_log (ticket_id, id);

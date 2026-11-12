-- Phòng R&D GĐ2: chỉ mục dẫn xuất của kho tri thức VX-Anh/aico-knowledge.
-- Nguồn gốc là thẻ Markdown trong Git; các bảng này dựng lại được bất cứ lúc nào từ main.
-- ref = 'main': tri thức đã duyệt. ref = '<mã ticket>': bản đề xuất (GĐ2.7), agent không đọc.
-- Không dùng pg_trgm: pgserver dùng trong test không có extension này; tìm gần đúng bằng
-- ILIKE trên search_text đã bỏ dấu.

create table kn_nodes (
    id text not null,
    ref text not null default 'main',
    kind text not null,
    title text not null,
    summary text not null,
    body_md text not null default '',
    tags jsonb not null default '[]',
    confidence smallint not null check (confidence between 1 and 3),
    version integer not null default 1,
    deprecated boolean not null default false,
    review_after date,
    card_path text not null,
    git_commit text,
    -- aico ghi: bỏ dấu, chữ thường (title + summary + tags + body)
    search_text text not null default '',
    search tsvector generated always as (to_tsvector('simple', search_text)) stored,
    indexed_at timestamptz not null default now(),
    primary key (id, ref)
);
create index kn_nodes_search_idx on kn_nodes using gin (search);

create table kn_edges (
    src text not null,
    rel text not null,
    dst text not null,
    ref text not null default 'main',
    source_ids jsonb not null default '[]',
    primary key (src, rel, dst, ref)
);
create index kn_edges_dst_idx on kn_edges (dst, ref);

create table kn_sources (
    node_id text not null,
    ref text not null default 'main',
    local_id text not null,
    url text not null,
    kind text not null,
    trust smallint not null default 2,
    published date,
    retrieved date,
    commit_sha text,
    quote text,
    quote_verified boolean,
    checked_at timestamptz,
    http_status integer,
    primary key (node_id, ref, local_id)
);
create index kn_sources_url_idx on kn_sources (url);

-- Mỗi lần agent nhận tri thức (chèn vào prompt, search, get): để đo thẻ nào có ích.
create table kn_usage (
    id bigserial primary key,
    at timestamptz not null default now(),
    ticket_id text,
    role text not null,
    kind text not null,
    query text,
    node_ids jsonb not null default '[]'
);

-- Commit của main đã dựng chỉ mục, để librarian chỉ dựng lại khi main đổi.
create table kn_builds (
    ref text primary key,
    git_commit text not null,
    nodes integer not null,
    skipped jsonb not null default '[]',
    built_at timestamptz not null default now()
);

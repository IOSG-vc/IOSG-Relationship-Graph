create schema if not exists deals;

create table if not exists deals.telegram_people (
    telegram_user_id bigint primary key,
    telegram_username text,
    display_name text,
    first_seen_at timestamptz not null default now(),
    last_synced_at timestamptz not null default now(),
    is_active boolean not null default true
);

create table if not exists deals.telegram_relationships (
    iosg_member text not null,
    telegram_user_id bigint not null references deals.telegram_people,
    incoming_count integer not null default 0,
    outgoing_count integer not null default 0,
    last_incoming_at timestamptz,
    last_outgoing_at timestamptz,
    last_interaction_at timestamptz,
    is_contact boolean not null default false,
    shared_group_count integer not null default 0,
    last_synced_at timestamptz not null default now(),
    primary key (iosg_member, telegram_user_id)
);

create table if not exists deals.telegram_contact_annotations (
    telegram_user_id bigint primary key references deals.telegram_people,
    company_name text,
    company_domain text,
    role text,
    x_username text,
    linkedin_url text,
    identity_status text not null default 'unverified'
        check (identity_status in ('unverified', 'probable', 'verified', 'rejected')),
    notes text,
    updated_by text,
    updated_at timestamptz not null default now()
);

create index if not exists telegram_annotations_company_name_idx
    on deals.telegram_contact_annotations (lower(company_name));

create index if not exists telegram_annotations_company_domain_idx
    on deals.telegram_contact_annotations (lower(company_domain));

create table if not exists deals.telegram_groups (
    telegram_group_id bigint primary key,
    title text,
    participant_count integer,
    approved_for_graph boolean not null default false,
    last_synced_at timestamptz not null default now()
);

create table if not exists deals.telegram_group_connections (
    iosg_member text not null,
    telegram_user_id bigint not null references deals.telegram_people,
    telegram_group_id bigint not null references deals.telegram_groups,
    replies_from_member integer not null default 0,
    replies_to_member integer not null default 0,
    last_interaction_at timestamptz,
    last_synced_at timestamptz not null default now(),
    primary key (iosg_member, telegram_user_id, telegram_group_id)
);

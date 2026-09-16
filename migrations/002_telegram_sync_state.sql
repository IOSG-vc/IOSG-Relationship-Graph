create table if not exists deals.telegram_sync_state (
    iosg_member text not null,
    peer_kind text not null check (peer_kind in ('direct', 'group')),
    telegram_peer_id bigint not null,
    last_message_id bigint not null check (last_message_id >= 0),
    updated_at timestamptz not null default now(),
    primary key (iosg_member, peer_kind, telegram_peer_id)
);

comment on table deals.telegram_sync_state is
    'Message-ID checkpoints only; never stores Telegram message contents or credentials.';

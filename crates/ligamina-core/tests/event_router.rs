use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use ligamina_contracts::PROTOCOL_VERSION;
use ligamina_core::{
    router::{EventRouter, RouterError, RouterLimits},
    store::{EventDisposition, EventKey, EventStatus, Store, StoreError},
};
use serde_json::{Value, json};
use tokio::{
    sync::{Semaphore, mpsc},
    time::{sleep, timeout},
};

fn event(id: &str, conversation_kind: &str, conversation_id: &str, message: &str) -> Value {
    json!({
        "schema_version": PROTOCOL_VERSION,
        "event_id": id,
        "trace_id": format!("trace-{id}"),
        "platform": "virtual.onebot",
        "account_id": "account-1",
        "conversation": {"kind": conversation_kind, "id": conversation_id},
        "actor_id": "user-1",
        "timestamp": "2026-10-10T00:00:00Z",
        "target_host": "worker-A",
        "message": [{"type": "text", "text": message}]
    })
}

fn key(client_id: &str, event: &Value) -> EventKey {
    EventKey {
        client_id: client_id.to_owned(),
        platform: event["platform"].as_str().unwrap().to_owned(),
        account_id: event["account_id"].as_str().unwrap().to_owned(),
        conversation_kind: event["conversation"]["kind"].as_str().unwrap().to_owned(),
        conversation_id: event["conversation"]["id"].as_str().unwrap().to_owned(),
        event_id: event["event_id"].as_str().unwrap().to_owned(),
    }
}

fn limits(max_pending_per_conversation: usize) -> RouterLimits {
    RouterLimits {
        max_pending_per_conversation,
        max_outstanding_events: 16,
        max_tracked_conversations: 16,
        max_concurrent_conversations: 2,
        processing_timeout: Duration::from_secs(2),
    }
}

async fn wait_for_status(router: &EventRouter, event_key: EventKey, expected: EventStatus) {
    timeout(Duration::from_secs(3), async {
        loop {
            if let Ok(record) = router.status(event_key.clone()).await
                && record.status == expected
            {
                return;
            }
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("event should reach expected state");
}

#[tokio::test]
async fn deduplicates_within_customer_conversation_scope_and_routes_once() {
    let store = Arc::new(Store::open_in_memory().expect("open store"));
    let calls = Arc::new(AtomicUsize::new(0));
    let routed_hosts = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let router = EventRouter::new(
        store,
        ["worker-A".to_owned(), "worker-B".to_owned()],
        limits(4),
        {
            let calls = calls.clone();
            let routed_hosts = routed_hosts.clone();
            move |host: String, _event: Value| {
                let calls = calls.clone();
                let routed_hosts = routed_hosts.clone();
                async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    routed_hosts.lock().await.push(host);
                    Ok(())
                }
            }
        },
    )
    .expect("valid router configuration");

    let original = event("evt-1", "group", "same-id", "hello");
    let (disposition, accepted) = router
        .dispatch("client-A".to_owned(), original.clone(), 1)
        .await
        .expect("accept event");
    assert_eq!(disposition, EventDisposition::Accepted);
    assert_eq!(accepted.status, EventStatus::Accepted);

    let mut retry = original.clone();
    retry["trace_id"] = json!("trace-retry");
    let (disposition, duplicate) = router
        .dispatch("client-A".to_owned(), retry, 2)
        .await
        .expect("same event payload is idempotent");
    assert_eq!(disposition, EventDisposition::Duplicate);
    assert_eq!(duplicate.key, accepted.key);

    let mut changed = original.clone();
    changed["message"][0]["text"] = json!("changed");
    assert!(matches!(
        router.dispatch("client-A".to_owned(), changed, 3).await,
        Err(RouterError::Store(StoreError::EventConflict))
    ));

    let mut wrong_route = event("evt-wrong-route", "group", "group-2", "hello");
    wrong_route["target_host"] = json!("unknown-host");
    assert!(matches!(
        router
            .dispatch("client-A".to_owned(), wrong_route, 4)
            .await,
        Err(RouterError::UnroutableHost(host)) if host == "unknown-host"
    ));

    wait_for_status(&router, key("client-A", &original), EventStatus::Completed).await;
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(&*routed_hosts.lock().await, &["worker-A"]);

    let private = event("evt-1", "private", "same-id", "private payload");
    let (disposition, private_record) = router
        .dispatch("client-A".to_owned(), private, 5)
        .await
        .expect("same event ID in a different conversation has its own scope");
    assert_eq!(disposition, EventDisposition::Accepted);
    assert_eq!(private_record.key.conversation_kind, "private");

    let same_group_other_client = router
        .dispatch("client-B".to_owned(), original, 6)
        .await
        .expect("same event in another client scope is independent");
    assert_eq!(same_group_other_client.0, EventDisposition::Accepted);
}

#[tokio::test]
async fn serializes_each_conversation_bounds_backlog_and_processes_other_sessions_in_parallel() {
    let store = Arc::new(Store::open_in_memory().expect("open store"));
    let (started_tx, mut started_rx) = mpsc::unbounded_channel::<String>();
    let gate = Arc::new(Semaphore::new(0));
    let active = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let router = EventRouter::new(store, ["worker-A".to_owned()], limits(2), {
        let gate = gate.clone();
        let active = active.clone();
        let peak = peak.clone();
        move |_host: String, event: Value| {
            let gate = gate.clone();
            let active = active.clone();
            let peak = peak.clone();
            let started_tx = started_tx.clone();
            async move {
                let current = active.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(current, Ordering::SeqCst);
                let id = event["event_id"].as_str().unwrap().to_owned();
                started_tx.send(id).unwrap();
                let permit = gate.acquire().await.map_err(|error| error.to_string())?;
                permit.forget();
                active.fetch_sub(1, Ordering::SeqCst);
                Ok(())
            }
        }
    })
    .expect("valid router configuration");

    let first = event("evt-a1", "group", "conversation-A", "hold");
    let same_conversation_next = event("evt-a2", "group", "conversation-A", "hold");
    let other_conversation = event("evt-b1", "group", "conversation-B", "hold");
    router
        .dispatch("client-A".to_owned(), first.clone(), 1)
        .await
        .expect("accept first event");
    assert_eq!(
        timeout(Duration::from_secs(1), started_rx.recv())
            .await
            .unwrap()
            .unwrap(),
        "evt-a1"
    );
    router
        .dispatch("client-A".to_owned(), same_conversation_next.clone(), 2)
        .await
        .expect("queue second event in same conversation");
    router
        .dispatch("client-A".to_owned(), other_conversation.clone(), 3)
        .await
        .expect("different conversation gets an independent worker");
    assert_eq!(
        timeout(Duration::from_secs(1), started_rx.recv())
            .await
            .unwrap()
            .unwrap(),
        "evt-b1"
    );

    let overloaded = event("evt-a3", "group", "conversation-A", "hold");
    assert!(matches!(
        router
            .dispatch("client-A".to_owned(), overloaded.clone(), 4)
            .await,
        Err(RouterError::Overloaded)
    ));
    assert!(matches!(
        router.status(key("client-A", &overloaded)).await,
        Err(RouterError::Store(StoreError::EventNotFound))
    ));

    gate.add_permits(2);
    assert_eq!(
        timeout(Duration::from_secs(1), started_rx.recv())
            .await
            .unwrap()
            .unwrap(),
        "evt-a2"
    );
    gate.add_permits(1);

    wait_for_status(&router, key("client-A", &first), EventStatus::Completed).await;
    wait_for_status(
        &router,
        key("client-A", &same_conversation_next),
        EventStatus::Completed,
    )
    .await;
    wait_for_status(
        &router,
        key("client-A", &other_conversation),
        EventStatus::Completed,
    )
    .await;
    assert_eq!(peak.load(Ordering::SeqCst), 2);
}

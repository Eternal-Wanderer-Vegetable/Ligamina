use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use ligamina_core::broker::{
    BrokerError, CapabilityBroker, CapabilityDescriptor, InvocationContext, MAX_CALL_DEPTH,
};
use serde_json::{Value, json};
use tokio::{sync::oneshot, time::sleep};

fn descriptor(
    capability_id: &str,
    input_schema: Value,
    output_schema: Value,
) -> CapabilityDescriptor {
    CapabilityDescriptor {
        capability_id: capability_id.to_owned(),
        input_schema,
        output_schema,
        pure: true,
    }
}

fn descriptor_fixture(name: &str) -> CapabilityDescriptor {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../contracts/v0.1/fixtures")
        .join(name);
    serde_json::from_slice(&std::fs::read(path).expect("capability fixture is readable"))
        .expect("fixture matches the broker descriptor structure")
}

#[tokio::test]
async fn active_host_can_route_a_schema_checked_call_to_another_host() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../samples/fixtures/cross-host/invoke-koishi-add.json"
    ))
    .unwrap();
    let broker =
        CapabilityBroker::new(|host_id, _epoch, capability_id, args, context| async move {
            assert_eq!(context.trace_id, "cross-host-fixture");
            assert_eq!(context.call_chain.last().unwrap().host_id, host_id);
            assert_eq!(capability_id, "ligamina.koishi.math.add");
            Ok(json!(
                args["left"].as_i64().unwrap() + args["right"].as_i64().unwrap()
            ))
        });
    broker
        .activate_host(
            fixture["caller"]["host_id"].as_str().unwrap(),
            fixture["caller"]["host_epoch"].as_u64().unwrap(),
        )
        .await
        .unwrap();
    broker
        .activate_host(
            fixture["target"]["host_id"].as_str().unwrap(),
            fixture["target"]["host_epoch"].as_u64().unwrap(),
        )
        .await
        .unwrap();
    broker
        .register_snapshot(
            "nonebot",
            7,
            vec![descriptor_fixture("capability.nonebot-add.json")],
        )
        .await
        .unwrap();
    broker
        .register_snapshot(
            "koishi",
            11,
            vec![descriptor_fixture("capability.koishi-add.json")],
        )
        .await
        .unwrap();
    let listed = broker.list().await;
    assert_eq!(listed.len(), 2);
    assert_eq!(
        listed[0].descriptor.capability_id,
        "ligamina.koishi.math.add"
    );
    assert_eq!(listed[0].host_id, "koishi");
    assert_eq!(listed[0].host_epoch, 11);

    let result = broker
        .invoke_from_host(
            fixture["caller"]["host_id"].as_str().unwrap(),
            fixture["caller"]["host_epoch"].as_u64().unwrap(),
            fixture["target"]["capability_id"].as_str().unwrap(),
            fixture["arguments"].clone(),
            InvocationContext::from_host(
                fixture["caller"]["host_id"].as_str().unwrap(),
                fixture["caller"]["host_epoch"].as_u64().unwrap(),
                fixture["trace_id"].as_str().unwrap(),
            ),
            Duration::from_secs(1),
        )
        .await
        .unwrap();
    assert_eq!(result, fixture["expected_result"]);

    let rejected = broker
        .invoke_from_host(
            "nonebot",
            7,
            "ligamina.koishi.math.add",
            json!({"left": "19", "right": 23}),
            InvocationContext::from_host("nonebot", 7, "bad-arguments"),
            Duration::from_secs(1),
        )
        .await;
    assert!(matches!(
        rejected,
        Err(BrokerError::InvalidArguments { .. })
    ));
}

#[tokio::test]
async fn broker_rejects_old_generations_and_cannot_reactivate_old_descriptors() {
    let broker = CapabilityBroker::new(|_, _, _, _, _| async { Ok(Value::Null) });
    broker.activate_host("koishi", 1).await.unwrap();
    broker
        .register("koishi", 1, descriptor("echo", json!(true), json!(true)))
        .await
        .unwrap();
    broker.activate_host("koishi", 2).await.unwrap();
    assert!(broker.list().await.is_empty());
    assert!(matches!(
        broker.activate_host("koishi", 1).await,
        Err(BrokerError::StaleEpoch { .. })
    ));

    assert!(matches!(
        broker
            .register("koishi", 1, descriptor("old", json!(true), json!(true)))
            .await,
        Err(BrokerError::StaleEpoch { .. })
    ));
    assert!(matches!(
        broker
            .invoke_root("echo", Value::Null, "stale", Duration::from_secs(1))
            .await,
        Err(BrokerError::CapabilityUnavailable(_))
    ));
}

#[tokio::test]
async fn broker_enforces_deadlines_and_discards_results_from_disconnected_hosts() {
    let broker = CapabilityBroker::new(|_, _, capability_id, _, _| async move {
        if capability_id == "slow" {
            sleep(Duration::from_millis(100)).await;
        }
        Ok(json!(1))
    });
    broker.activate_host("nonebot", 1).await.unwrap();
    broker.activate_host("koishi", 2).await.unwrap();
    broker
        .register(
            "koishi",
            2,
            descriptor("slow", json!(true), json!({"type":"integer"})),
        )
        .await
        .unwrap();

    assert_eq!(
        broker
            .invoke_from_host(
                "nonebot",
                1,
                "slow",
                Value::Null,
                InvocationContext::from_host("nonebot", 1, "timeout"),
                Duration::from_millis(5)
            )
            .await,
        Err(BrokerError::DeadlineExceeded("slow".to_owned()))
    );

    broker
        .register(
            "koishi",
            2,
            descriptor("fast", json!(true), json!({"type":"integer"})),
        )
        .await
        .unwrap();
    // A newer activation also proves an in-flight result cannot cross generations.
    let (started_tx, started_rx) = oneshot::channel();
    let (finish_tx, finish_rx) = oneshot::channel::<()>();
    let started_tx = Arc::new(Mutex::new(Some(started_tx)));
    let finish_rx = Arc::new(tokio::sync::Mutex::new(Some(finish_rx)));
    let delayed = CapabilityBroker::new(move |_, _, _, _, _| {
        let started_tx = started_tx.clone();
        let finish_rx = finish_rx.clone();
        async move {
            if let Some(tx) = started_tx.lock().unwrap().take() {
                let _ = tx.send(());
            }
            if let Some(rx) = finish_rx.lock().await.take() {
                let _ = rx.await;
            }
            Ok(json!(1))
        }
    });
    delayed.activate_host("nonebot", 1).await.unwrap();
    delayed.activate_host("koishi", 2).await.unwrap();
    delayed
        .register(
            "koishi",
            2,
            descriptor("fast", json!(true), json!({"type":"integer"})),
        )
        .await
        .unwrap();
    let invoking = {
        let delayed = delayed.clone();
        tokio::spawn(async move {
            delayed
                .invoke_from_host(
                    "nonebot",
                    1,
                    "fast",
                    Value::Null,
                    InvocationContext::from_host("nonebot", 1, "disconnect"),
                    Duration::from_secs(2),
                )
                .await
        })
    };
    started_rx.await.unwrap();
    delayed.deactivate_host("koishi", 2).await;
    let _ = finish_tx.send(());
    assert_eq!(
        invoking.await.unwrap(),
        Err(BrokerError::HostUnavailable("koishi".to_owned()))
    );
}

#[tokio::test]
async fn broker_rejects_recursive_capability_loops_and_excessive_depth() {
    let broker_slot = Arc::new(Mutex::new(None::<CapabilityBroker>));
    let slot = broker_slot.clone();
    let broker = CapabilityBroker::new(move |host_id, epoch, capability_id, args, context| {
        let broker = slot.lock().unwrap().as_ref().unwrap().clone();
        async move {
            if capability_id == "loop" {
                return broker
                    .invoke_from_host(
                        &host_id,
                        epoch,
                        "loop",
                        args,
                        context,
                        Duration::from_secs(1),
                    )
                    .await;
            }
            if let Some(index) = capability_id
                .strip_prefix("depth.")
                .and_then(|value| value.parse::<usize>().ok())
            {
                if index + 1 == MAX_CALL_DEPTH {
                    return Ok(Value::Null);
                }
                return broker
                    .invoke_from_host(
                        &host_id,
                        epoch,
                        &format!("depth.{}", index + 1),
                        args,
                        context,
                        Duration::from_secs(1),
                    )
                    .await;
            }
            Ok(Value::Null)
        }
    });
    *broker_slot.lock().unwrap() = Some(broker.clone());
    broker.activate_host("nonebot", 3).await.unwrap();
    for capability_id in std::iter::once("loop".to_owned())
        .chain((0..=MAX_CALL_DEPTH).map(|index| format!("depth.{index}")))
    {
        broker
            .register(
                "nonebot",
                3,
                descriptor(&capability_id, json!(true), json!(true)),
            )
            .await
            .unwrap();
    }

    assert!(matches!(
        broker
            .invoke_from_host(
                "nonebot",
                3,
                "loop",
                Value::Null,
                InvocationContext::from_host("nonebot", 3, "loop"),
                Duration::from_secs(1)
            )
            .await,
        Err(BrokerError::LoopDetected(_))
    ));
    assert_eq!(
        broker
            .invoke_from_host(
                "nonebot",
                3,
                "depth.0",
                Value::Null,
                InvocationContext::from_host("nonebot", 3, "depth"),
                Duration::from_secs(1)
            )
            .await,
        Err(BrokerError::MaxCallDepth)
    );
}

#[tokio::test]
async fn broker_rejects_invalid_schemas_non_pure_descriptors_and_inactive_hosts() {
    let broker = CapabilityBroker::new(|_, _, _, _, _| async { Ok(Value::Null) });
    assert!(matches!(
        broker
            .register("nonebot", 1, descriptor("absent", json!(true), json!(true)))
            .await,
        Err(BrokerError::HostUnavailable(_))
    ));
    broker.activate_host("nonebot", 1).await.unwrap();
    let mut impure = descriptor("impure", json!(true), json!(true));
    impure.pure = false;
    assert!(matches!(
        broker.register("nonebot", 1, impure).await,
        Err(BrokerError::InvalidDescriptor(_))
    ));
    assert!(matches!(
        broker
            .register(
                "nonebot",
                1,
                descriptor("bad-schema", json!({"type":"wat"}), json!(true))
            )
            .await,
        Err(BrokerError::InvalidDescriptor(_))
    ));
}

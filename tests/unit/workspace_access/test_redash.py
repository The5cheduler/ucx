import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from databricks.sdk.core import DatabricksError
from databricks.sdk.errors import InternalError, NotFound, PermissionDenied
from databricks.sdk.service import sql

from databricks.labs.ucx.workspace_access.redash import (
    Listing,
    Permissions,
    RedashPermissionsSupport,
)


def test_crawlers():
    ws = MagicMock()

    ws.alerts.list.return_value = [
        sql.Alert(
            id="test",
        )
    ]
    ws.queries.list.return_value = [
        sql.Query(
            id="test",
        )
    ]
    ws.dashboards.list.return_value = [sql.Dashboard(id="test")]

    sample_acl = [
        sql.AccessControl(
            group_name="test",
            permission_level=sql.PermissionLevel.CAN_MANAGE,
        )
    ]

    ws.dbsql_permissions.get.side_effect = [
        sql.GetResponse(object_type=ot, object_id="test", access_control_list=sample_acl)
        for ot in [sql.ObjectType.ALERT, sql.ObjectType.QUERY, sql.ObjectType.DASHBOARD]
    ]

    sup = RedashPermissionsSupport(
        ws=ws,
        listings=[
            Listing(ws.alerts.list, sql.ObjectTypePlural.ALERTS),
            Listing(ws.dashboards.list, sql.ObjectTypePlural.DASHBOARDS),
            Listing(ws.queries.list, sql.ObjectTypePlural.QUERIES),
        ],
    )

    tasks = list(sup.get_crawler_tasks())
    assert len(tasks) == 3
    ws.alerts.list.assert_called_once()
    ws.queries.list.assert_called_once()
    ws.dashboards.list.assert_called_once()
    for task in tasks:
        item = task()
        assert item.object_id == "test"
        assert item.object_type in ["alerts", "dashboards", "queries"]
        assert item.raw is not None


def test_apply(migration_state):
    ws = MagicMock()
    ws.dbsql_permissions.get.return_value = sql.GetResponse(
        object_type=sql.ObjectType.ALERT,
        object_id="test",
        access_control_list=[
            sql.AccessControl(
                group_name="test",
                permission_level=sql.PermissionLevel.CAN_MANAGE,
            ),
            sql.AccessControl(
                group_name="irrelevant",
                permission_level=sql.PermissionLevel.CAN_MANAGE,
            ),
        ],
    )
    ws.dbsql_permissions.set.return_value = sql.GetResponse(
        object_type=sql.ObjectType.ALERT,
        object_id="test",
        access_control_list=[
            sql.AccessControl(
                group_name="test",
                permission_level=sql.PermissionLevel.CAN_MANAGE,
            ),
            sql.AccessControl(
                group_name="irrelevant",
                permission_level=sql.PermissionLevel.CAN_MANAGE,
            ),
        ],
    )
    sup = RedashPermissionsSupport(ws=ws, listings=[])
    item = Permissions(
        object_id="test",
        object_type="alerts",
        raw=json.dumps(
            sql.GetResponse(
                object_type=sql.ObjectType.ALERT,
                object_id="test",
                access_control_list=[
                    sql.AccessControl(
                        group_name="test",
                        permission_level=sql.PermissionLevel.CAN_MANAGE,
                    ),
                    sql.AccessControl(
                        group_name="irrelevant",
                        permission_level=sql.PermissionLevel.CAN_MANAGE,
                    ),
                ],
            ).as_dict()
        ),
    )
    task = sup.get_apply_task(item, migration_state)
    task()
    assert ws.dbsql_permissions.set.call_count == 1
    expected_payload = [
        sql.AccessControl(
            group_name="test",
            permission_level=sql.PermissionLevel.CAN_MANAGE,
        ),
        sql.AccessControl(
            group_name="irrelevant",
            permission_level=sql.PermissionLevel.CAN_MANAGE,
        ),
    ]
    ws.dbsql_permissions.set.assert_called_once_with(
        object_type=sql.ObjectTypePlural.ALERTS, object_id="test", access_control_list=expected_payload
    )


def test_safe_getter_known():
    ws = MagicMock()
    ws.dbsql_permissions.get.side_effect = NotFound(...)
    sup = RedashPermissionsSupport(ws=ws, listings=[])
    assert sup._safe_get_dbsql_permissions(object_type=sql.ObjectTypePlural.ALERTS, object_id="test") is None


def test_safe_getter_unknown():
    ws = MagicMock()
    ws.dbsql_permissions.get.side_effect = InternalError(...)
    sup = RedashPermissionsSupport(ws=ws, listings=[])
    with pytest.raises(DatabricksError):
        sup._safe_get_dbsql_permissions(object_type=sql.ObjectTypePlural.ALERTS, object_id="test")


def test_empty_permissions():
    ws = MagicMock()
    ws.dbsql_permissions.get.side_effect = NotFound(...)
    sup = RedashPermissionsSupport(ws=ws, listings=[])
    assert sup._crawler_task(object_id="test", object_type=sql.ObjectTypePlural.ALERTS) is None


def test_applier_task_should_return_true_if_permission_is_up_to_date():
    ws = MagicMock()
    acl_grp_1 = sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE)
    acl_grp_2 = sql.AccessControl(group_name="group_2", permission_level=sql.PermissionLevel.CAN_MANAGE)
    ws.dbsql_permissions.get.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[acl_grp_1, acl_grp_2],
    )
    ws.dbsql_permissions.set.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[acl_grp_1, acl_grp_2],
    )

    sup = RedashPermissionsSupport(ws=ws, listings=[])
    result = sup._applier_task(sql.ObjectTypePlural.QUERIES, "test", [acl_grp_1])
    assert result


def test_applier_task_should_return_true_if_permission_is_up_to_date_with_multiple_permissions():
    ws = MagicMock()
    acl_1_grp_1 = sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE)
    acl_2_grp_1 = sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN)
    acl_3_grp_1 = sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN)
    acl_grp_2 = sql.AccessControl(group_name="group_2", permission_level=sql.PermissionLevel.CAN_MANAGE)
    ws.dbsql_permissions.get.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[acl_1_grp_1, acl_2_grp_1, acl_3_grp_1, acl_grp_2],
    )
    ws.dbsql_permissions.set.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[acl_1_grp_1, acl_2_grp_1, acl_3_grp_1, acl_grp_2],
    )

    sup = RedashPermissionsSupport(ws=ws, listings=[])
    result = sup._applier_task(sql.ObjectTypePlural.QUERIES, "test", [acl_1_grp_1, acl_2_grp_1])
    assert result


def test_applier_task_failed():
    ws = MagicMock()
    ws.dbsql_permissions.get.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[
            sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE),
            sql.AccessControl(group_name="group_2", permission_level=sql.PermissionLevel.CAN_RUN),
        ],
    )

    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    with pytest.raises(TimeoutError) as e:
        sup._applier_task(
            sql.ObjectTypePlural.QUERIES,
            "test",
            [sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN)],
        )
    assert "Timed out after" in str(e.value)


def test_applier_task_failed_when_all_permissions_not_up_to_date():
    ws = MagicMock()
    ws.dbsql_permissions.get.return_value = sql.GetResponse(
        object_type=sql.ObjectType.QUERY,
        object_id="test",
        access_control_list=[
            sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE),
            sql.AccessControl(group_name="group_2", permission_level=sql.PermissionLevel.CAN_RUN),
        ],
    )

    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    with pytest.raises(TimeoutError) as e:
        sup._applier_task(
            sql.ObjectTypePlural.QUERIES,
            "test",
            [
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN),
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE),
            ],
        )
    assert "Timed out after" in str(e.value)


def test_applier_task_when_set_error_non_retriable():
    ws = MagicMock()
    error_code = "PERMISSION_DENIED"
    ws.dbsql_permissions.set.side_effect = DatabricksError(error_code=error_code)

    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    with pytest.raises(TimeoutError) as e:
        sup._applier_task(
            sql.ObjectTypePlural.QUERIES,
            "test",
            [
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN),
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE),
            ],
        )
    assert "Timed out after" in str(e.value)


def test_applier_task_when_set_error_retriable():
    ws = MagicMock()
    error_code = "INTERNAL_SERVER_ERROR"
    ws.dbsql_permissions.set.side_effect = DatabricksError(error_code=error_code)

    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    with pytest.raises(TimeoutError) as e:
        sup._applier_task(
            sql.ObjectTypePlural.QUERIES,
            "test",
            [
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_RUN),
                sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE),
            ],
        )
    assert "Timed out after" in str(e.value)


def test_safe_set_permissions_when_error_non_retriable():
    ws = MagicMock()
    ws.dbsql_permissions.set.side_effect = PermissionDenied(...)
    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    acl = [sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE)]
    result = sup._safe_set_permissions(sql.ObjectTypePlural.QUERIES, "test", acl)
    assert result is None


def test_safe_set_permissions_when_error_retriable():
    ws = MagicMock()
    ws.dbsql_permissions.set.side_effect = InternalError(...)
    sup = RedashPermissionsSupport(ws=ws, listings=[], verify_timeout=timedelta(seconds=1))
    acl = [sql.AccessControl(group_name="group_1", permission_level=sql.PermissionLevel.CAN_MANAGE)]
    with pytest.raises(InternalError) as e:
        sup._safe_set_permissions(sql.ObjectTypePlural.QUERIES, "test", acl)
    assert e.type == InternalError

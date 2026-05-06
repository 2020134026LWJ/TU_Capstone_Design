"""
인프라 sanity 체크 — 픽스처가 정상적으로 RequestHandler를 만드는지 확인.
실제 알고리즘 동작 검증은 별도 test_*.py에서 수행.
"""

from server.robot_manager import RobotStatus


def test_handler_fixture_constructs(handler):
    """RequestHandler가 모든 매니저를 끼고 정상 인스턴스화"""
    assert handler.config is not None
    assert handler.path_planner is not None
    assert handler.robot_manager is not None
    assert handler.shelf_manager is not None
    assert handler.staging_manager is not None
    assert handler.task_manager is not None


def test_robot_config_loaded(handler):
    """robot_config.json 기준 AGV 2대가 home 노드에 있음"""
    agv1 = handler.robot_manager.get_robot(1)
    agv2 = handler.robot_manager.get_robot(2)
    assert agv1 is not None
    assert agv2 is not None
    assert agv1.home_node == 9
    assert agv2.home_node == 33
    assert agv1.current_node == 9
    assert agv2.current_node == 33
    assert agv1.status == RobotStatus.IDLE
    assert agv2.status == RobotStatus.IDLE


def test_corridors_initialized(handler):
    """shelf_config.json 기준 W1(33), W2(9) corridor가 등록됨"""
    corridors = handler.staging_manager.corridors
    assert 33 in corridors
    assert 9 in corridors


def test_mock_mqtt_records_publish(mock_mqtt):
    """Mock의 publish_cmd가 호출 기록을 남김"""
    mock_mqtt.publish_cmd(1, "forward")
    mock_mqtt.publish_cmd(1, "turn_left")
    assert mock_mqtt.cmds == [(1, "forward"), (1, "turn_left")]
    assert mock_mqtt.last_cmd(1) == "turn_left"
    assert mock_mqtt.cmds_for(2) == []


def test_helpers_init_heading(handler, helpers):
    """init_robot_heading 헬퍼가 heading_initialized 플래그를 켜줌"""
    helpers.init_robot_heading(handler, rid=1, heading=90)
    robot = handler.robot_manager.get_robot(1)
    assert robot.heading == 90
    assert robot.heading_initialized is True

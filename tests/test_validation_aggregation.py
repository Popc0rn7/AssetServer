from assetserver.scene_job_handlers import _aggregate_penetration_contacts


def test_penetrations_are_aggregated_by_stable_object_pair():
    contacts = [("lamp", "desk", index / 1000) for index in range(1, 101)]
    contacts += [("desk", "lamp", 0.05), ("chair", "chair", 2.0)]

    issues = _aggregate_penetration_contacts(contacts)

    assert len(issues) == 1
    assert issues[0]["code"] == "penetration"
    assert issues[0]["object_ids"] == ["desk", "lamp"]
    assert issues[0]["metric"] == 0.1
    assert issues[0]["metadata"] == {"contact_count": 101}


def test_penetration_report_order_is_stable():
    issues = _aggregate_penetration_contacts(
        [("z", "a", 0.2), ("c", "b", 0.1), ("a", "z", 0.3)]
    )

    assert [issue["object_ids"] for issue in issues] == [["a", "z"], ["b", "c"]]
    assert issues[0]["metric"] == 0.3

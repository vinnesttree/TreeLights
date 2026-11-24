import time

from treelights.progress import Progress, read, sub


def test_progress_file_steps_throttling_and_stages(tmp_path):
    p = tmp_path / "job.json"
    prog = Progress(p)
    r = prog.part(0.2, 0.6, "Step 2 of 5: Finding the LEDs")
    assert read(p)["step"] == "Step 2 of 5: Finding the LEDs" and read(p)["fraction"] == 0.2
    r(0.5, "10 of 20 frames")
    assert abs(read(p)["fraction"] - 0.4) < 1e-9
    r(0.6, "12 of 20 frames")  # a counter within 0.5 s: throttled
    assert read(p)["detail"] == "10 of 20 frames"
    r(0.6, "Building LED hypotheses")  # a new stage: shown at once
    assert read(p)["detail"] == "Building LED hypotheses"
    s = sub(r, 0.5, 1.0, "reading the codes")
    time.sleep(0.55)
    s(0.5)
    d = read(p)
    assert abs(d["fraction"] - (0.2 + 0.4 * 0.75)) < 1e-9 and d["detail"] == "reading the codes"
    assert read(tmp_path / "missing.json") is None
    Progress(None).part(0, 1, "x")(0.5, "nothing is written")

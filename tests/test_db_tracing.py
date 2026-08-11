from loop_orchestrator import db as dbmod

TOK = {"input": 10, "cache_write": 2_000, "cache_read": 40_000, "output": 300}


async def _run(db):
    return await dbmod.create_run(db, "o/r", 5, "feat/x", pr_title="feat: x")


async def test_a_run_without_a_trace_has_no_rollup(db):
    run = await _run(db)
    assert await dbmod.trace_rollup_for_run(db, run.id) is None


async def test_stage_costs_sum_into_the_run_total(db):
    run = await _run(db)
    await dbmod.save_stage_cost(db, run.id, "execute", "claude-opus-5", True,
                                200, 90, TOK, 12.5)
    await dbmod.save_stage_cost(db, run.id, "review", "claude-fable-5", True,
                                50, 20, TOK, 7.5)
    await dbmod.refresh_run_trace(db, run.id, "t" * 32)

    roll = await dbmod.trace_rollup_for_run(db, run.id)
    assert roll["trace_id"] == "t" * 32
    assert roll["api_calls"] == 250 and roll["tool_calls"] == 110
    assert roll["cost_usd"] == 20.0
    assert roll["tokens_cache_read"] == 80_000
    assert [s["stage"] for s in roll["stages"]] == ["execute", "review"]
    assert roll["stages"][0]["model"] == "claude-opus-5"
    assert roll["stages"][0]["fresh"] == 1


async def test_a_second_pass_over_a_stage_replaces_it(db):
    # A revise sends the Run back through execute/review/e2e. The second pass is
    # the one that shipped; the numbers must not accumulate.
    run = await _run(db)
    await dbmod.save_stage_cost(db, run.id, "execute", "claude-opus-5", True,
                                200, 90, TOK, 12.5)
    await dbmod.save_stage_cost(db, run.id, "execute", "claude-opus-5", False,
                                10, 4, {"input": 1}, 0.5)
    await dbmod.refresh_run_trace(db, run.id, "t" * 32)

    roll = await dbmod.trace_rollup_for_run(db, run.id)
    assert len(roll["stages"]) == 1
    assert roll["api_calls"] == 10 and roll["cost_usd"] == 0.5
    assert roll["stages"][0]["fresh"] == 0


async def test_an_unknown_fresh_flag_round_trips_as_null(db):
    run = await _run(db)
    await dbmod.save_stage_cost(db, run.id, "e2e", "", None, 1, 0, {}, 0.0)
    await dbmod.refresh_run_trace(db, run.id, "t" * 32)
    roll = await dbmod.trace_rollup_for_run(db, run.id)
    assert roll["stages"][0]["fresh"] is None


async def test_refresh_is_idempotent(db):
    run = await _run(db)
    await dbmod.save_stage_cost(db, run.id, "execute", "m", True, 3, 1, TOK, 1.0)
    await dbmod.refresh_run_trace(db, run.id, "t" * 32)
    await dbmod.refresh_run_trace(db, run.id, "t" * 32)
    roll = await dbmod.trace_rollup_for_run(db, run.id)
    assert roll["api_calls"] == 3 and roll["cost_usd"] == 1.0


async def test_two_runs_keep_separate_rollups(db):
    a, b = await _run(db), await _run(db)
    await dbmod.save_stage_cost(db, a.id, "execute", "m", True, 5, 2, TOK, 1.0)
    await dbmod.save_stage_cost(db, b.id, "execute", "m", True, 7, 3, TOK, 2.0)
    await dbmod.refresh_run_trace(db, a.id, "a" * 32)
    await dbmod.refresh_run_trace(db, b.id, "b" * 32)
    assert (await dbmod.trace_rollup_for_run(db, a.id))["api_calls"] == 5
    assert (await dbmod.trace_rollup_for_run(db, b.id))["api_calls"] == 7

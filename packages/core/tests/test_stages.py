from __future__ import annotations

from itertools import pairwise

import pytest

from jobtrack_core.domain.stages import (
    ALLOWED_TRANSITIONS,
    PIPELINE,
    TERMINAL,
    IllegalTransitionError,
    Stage,
    can_transition,
    is_active,
    pipeline_index,
    transition,
)


class TestPipelineShape:
    def test_every_stage_appears_in_exactly_one_of_pipeline_or_terminal(self) -> None:
        assert set(PIPELINE) | TERMINAL == set(Stage)
        assert not set(PIPELINE) & TERMINAL

    def test_every_stage_has_a_transition_entry(self) -> None:
        # A missing entry would be a KeyError at the worst possible moment.
        assert set(ALLOWED_TRANSITIONS) == set(Stage)


class TestForwardMovement:
    @pytest.mark.parametrize(
        ("current", "nxt"), [(PIPELINE[i], PIPELINE[i + 1]) for i in range(len(PIPELINE) - 1)]
    )
    def test_each_stage_advances_to_the_next(self, current: Stage, nxt: Stage) -> None:
        assert transition(current, nxt) == transition(current, nxt)
        assert can_transition(current, nxt)

    def test_stages_cannot_be_skipped(self) -> None:
        # Applied straight to Onsite would leave Screen and Technical looking like
        # stages nobody reached, which is indistinguishable in the funnel from
        # stages everyone failed.
        with pytest.raises(IllegalTransitionError):
            transition(Stage.APPLIED, Stage.ONSITE)

    def test_offer_is_the_end_of_the_pipeline(self) -> None:
        assert can_transition(Stage.OFFER, Stage.REJECTED)
        assert not any(can_transition(Stage.OFFER, s) for s in PIPELINE if s is not Stage.ONSITE)


class TestBackwardMovement:
    def test_one_step_back_is_allowed_because_people_mis_click(self) -> None:
        assert can_transition(Stage.TECHNICAL, Stage.SCREEN)

    def test_two_steps_back_is_not(self) -> None:
        assert not can_transition(Stage.TECHNICAL, Stage.APPLIED)

    def test_saved_has_nowhere_to_go_back_to(self) -> None:
        assert not can_transition(Stage.SAVED, Stage.SAVED)


class TestTerminalStates:
    @pytest.mark.parametrize("terminal", sorted(TERMINAL))
    def test_every_active_stage_can_end(self, terminal: Stage) -> None:
        for stage in PIPELINE:
            assert can_transition(stage, terminal), f"{stage} cannot reach {terminal}"

    @pytest.mark.parametrize("terminal", sorted(TERMINAL))
    def test_nothing_leaves_a_terminal_state(self, terminal: Stage) -> None:
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        for target in Stage:
            assert not can_transition(terminal, target)

    def test_reopening_a_rejection_is_refused_with_a_useful_message(self) -> None:
        with pytest.raises(IllegalTransitionError) as caught:
            transition(Stage.REJECTED, Stage.SCREEN)

        assert "terminal" in str(caught.value)
        assert caught.value.current is Stage.REJECTED
        assert caught.value.requested is Stage.SCREEN


class TestSelfTransition:
    @pytest.mark.parametrize("stage", sorted(Stage))
    def test_a_stage_never_transitions_to_itself(self, stage: Stage) -> None:
        # Otherwise a repeated drag writes a stage_events row that resets
        # stage_entered_at, and the nudge clock restarts for no reason.
        assert not can_transition(stage, stage)

    def test_the_error_says_so_plainly(self) -> None:
        with pytest.raises(IllegalTransitionError, match="already in applied"):
            transition(Stage.APPLIED, Stage.APPLIED)


class TestHelpers:
    def test_pipeline_stages_are_active_and_terminal_ones_are_not(self) -> None:
        assert all(is_active(s) for s in PIPELINE)
        assert not any(is_active(s) for s in TERMINAL)

    def test_pipeline_index_orders_the_funnel(self) -> None:
        assert pipeline_index(Stage.SAVED) == 0
        assert pipeline_index(Stage.OFFER) == len(PIPELINE) - 1

    def test_terminal_stages_have_no_pipeline_position(self) -> None:
        assert pipeline_index(Stage.GHOSTED) is None


def test_a_realistic_journey_is_legal_end_to_end() -> None:
    journey = [
        Stage.SAVED,
        Stage.APPLIED,
        Stage.SCREEN,
        Stage.TECHNICAL,
        Stage.ONSITE,
        Stage.OFFER,
    ]
    for current, nxt in pairwise(journey):
        assert transition(current, nxt).to_stage is nxt


def test_the_common_sad_path_is_legal() -> None:
    assert transition(Stage.APPLIED, Stage.GHOSTED).to_stage is Stage.GHOSTED

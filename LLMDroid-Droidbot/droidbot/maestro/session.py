from typing import Optional

from .schemas import Budget, GoalSpec, GoalState


def create_goal_state(
    run_id: str,
    goal: GoalSpec,
    state_version: int = 0,
    budget: Optional[Budget] = None,
) -> GoalState:
    return GoalState(
        run_id=run_id,
        goal_id=goal.goal_id,
        goal_description=goal.description,
        state_version=state_version,
        subgoals=goal.subgoals,
        budget=budget or Budget(),
    )

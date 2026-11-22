import sys
sys.path.insert(0, "/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b")
import os
os.chdir("/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b")
from agent.turn_explainers import TurnExplainersMixin, _persistence_explanation_key
from hermes_state_errors import classify_persistence_error, PERSISTENCE_ERROR_CAUSES
print("cause:", classify_persistence_error("owner_unavailable"))
print("in tuple:", "owner_unavailable" in PERSISTENCE_ERROR_CAUSES)
print("key:", _persistence_explanation_key("owner_unavailable"))
for cause in ("owner_unavailable", "unknown"):
    print("---", cause)
    print(TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", cause))

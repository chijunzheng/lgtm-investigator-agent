# Chain-of-thought judge prompt: forces the LLM to analyze the diagnosis in
# structured steps (Step 1: analysis, Step 2: scoring) before assigning scores.
# This improves scoring consistency vs. directly asking for numbers.
#
# The 1-5 rubrics are calibrated so that:
#   5 = production-quality diagnosis
#   3 = correct service but weak evidence
#   1 = completely wrong or no diagnosis
#
# Response format is JSON for reliable automated parsing.
DIAGNOSIS_JUDGE_PROMPT = """You are evaluating an incident investigation agent's diagnosis.

## Scenario
Symptom: {symptom}
Expected root cause service: {expected_root_cause}
Expected signal: {expected_signal}

## Agent's Response
{agent_response}

## Your Task

Evaluate the agent's diagnosis in two steps.

### Step 1: Analysis

First, analyze the agent's response by answering these questions:

1. **Root cause identification**: What service did the agent identify as the root cause? Does it match the expected root cause service "{expected_root_cause}"? Did the agent describe the correct failure mechanism, or a different one?

2. **Evidence cited**: List each piece of specific evidence the agent cited. For each, note which signal type it came from (logs, metrics, or traces). Count how many distinct signal types were used.

3. **Reasoning chain**: Does the agent show a clear path from the reported symptom to the root cause? Are there logical gaps or jumps in reasoning?

### Step 2: Scoring

Based on your analysis above, rate each dimension 1-5:

**root_cause_accuracy**:
5 = Correctly identifies the exact service AND failure mechanism
4 = Correct service, partially correct mechanism
3 = Correct service, wrong mechanism
2 = Related service (downstream/upstream of actual root cause)
1 = Wrong service or no diagnosis

**evidence_quality**:
5 = Cites specific data from all 3 signal types (logs, metrics, traces)
4 = Cites specific data from 2 signal types
3 = Cites data from 1 signal type
2 = References signals vaguely without specific data
1 = No evidence cited

**reasoning_quality**:
5 = Clear causal chain from symptom -> investigation -> root cause
4 = Good reasoning with minor gaps
3 = Reaches right conclusion but reasoning has logical gaps
2 = Disorganized reasoning
1 = No visible reasoning process

Respond in JSON with your analysis first, then scores:
{{"analysis": "your step 1 analysis here", "root_cause_accuracy": N, "evidence_quality": N, "reasoning_quality": N, "explanation": "one sentence summary"}}"""

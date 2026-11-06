# Integrating Memory into a Deep Research Agent

The integration layer is intentionally small and framework-free. It exposes
memory as tools and shows a plan/search/synthesize loop that can be replaced by
an LLM planner.

## MemoryToolbox

```python
from engram import MemoryConfig, MemoryService
from engram.integrations import MemoryToolbox

service = MemoryService(MemoryConfig.from_env())
toolbox = MemoryToolbox(service, user_id="u1")

context = toolbox.search_context("my language preference")
records = toolbox.search("Agent internship")
record = toolbox.write("The user prefers Chinese explanations.", importance=0.8)

tools = toolbox.tools()
# {"memory_search": callable, "memory_write": callable}
```

`memory_search(query) -> str` returns the prompt-ready context block.
`memory_write(content) -> str` returns the new memory id.

## DeepResearchAgent

```python
from engram.integrations import DeepResearchAgent

agent = DeepResearchAgent(service, user_id="u1")
report = agent.run("What should the user prepare next?")

print(report.context)
print(report.evidence)
print(report.conclusion)
print(report.written_memory_id)
```

The default loop:

1. retrieve relevant long-term memories;
2. generate two subquestions;
3. use a pluggable `searcher` callback (web search, document index, P0 harness);
4. synthesize a conclusion;
5. write the conclusion back to memory with `metadata.kind =
   "research_conclusion"`.

## Plugging an external searcher

```python
agent = DeepResearchAgent(
    service,
    user_id="u1",
    searcher=lambda query: my_web_search(query),
)
```

This is the seam for the P0 Deep Research project: replace the deterministic
planner and `_compose` with an LLM, and pass a real search tool.

## Example

```bash
python examples/deep_research_memory.py
```

The example writes a background memory, runs the agent, prints the report, and
shows the stored conclusion.

## Limitations

- The planner and synthesis are deterministic; this is an integration skeleton,
  not a production research agent.
- The default searcher is memory-only; without external evidence, the agent says
  it lacks evidence rather than inventing it.
- There is no tool sandbox in this layer; wrap external tools with your own
  permission/allowlist policy.
- The P0 agent should own the LLM loop; this module only owns the memory
  contract.

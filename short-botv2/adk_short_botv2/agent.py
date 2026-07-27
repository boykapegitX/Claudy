from google.adk.agents import Agent

from adk_short_botv2.prompt import ROOT_AGENT_INSTRUCTION
from adk_short_botv2.tools import count_characters

root_agent = Agent(
    name="adk_short_botv2",
    model="gemini-2.0-flash",
    description="A bot that shortens messages while maintaining their core meaning",
    instruction=ROOT_AGENT_INSTRUCTION,
    tools=[count_characters],
)

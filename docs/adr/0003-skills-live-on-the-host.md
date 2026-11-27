# Skills live on the Host, not in the Hub

The source of truth for a **Skill** is the **Host**'s **Skill store**. Installing it for an **Agent** makes it available in that Agent's working directory, and Kimi loads only that directory. The **Hub**'s skills table is a heartbeat-fed cache used for the UI and for choosing which Skills to send with a **Job**; keeping the source on the Host avoids shipping code through the Hub and lets one Host share Skills across its Agents.

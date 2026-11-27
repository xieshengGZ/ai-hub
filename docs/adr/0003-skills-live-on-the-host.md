# Skills live on the Host, not in the Hub

The source of truth for a **Skill** is the **Host**'s **Skill store**. Installing it for an **Agent** links it into that Agent's working directory, and the Daemon mirrors those links into the Kimi home directory that ACP actually scans. The **Hub**'s skills table is a heartbeat-fed cache used for the UI and for choosing which Skills to send with a **Job**; keeping the source on the Host avoids shipping code through the Hub and lets one Host share Skills across its Agents.

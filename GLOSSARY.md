# AI Hub

A self-hosted workbench that turns AI agents on multiple machines into one web conversation surface. A **Hub** coordinates work; each **Host** runs a **Daemon** that executes it locally.

## Language

### Topology

**Hub**:
The single server that owns the web app, database, and files; it coordinates **Agents**, **Jobs**, and **Conversations** but never connects to a **Host**.
_Avoid_: server, central node, control plane

**Host**:
A machine that runs a **Daemon** and the **Agents** on it.
_Avoid_: node, machine, server

**Daemon**:
The long-lived process on a **Host** that executes **Jobs** and reports to the **Hub**.
_Avoid_: worker, runner, agent (bare)

### Work

**Agent**:
A configured AI worker on one **Host**; it owns one **Conversation**.
_Avoid_: bot, assistant, worker

**Conversation**:
The single chat thread between the user and one **Agent**.
_Avoid_: session, chat, thread

**Message**:
One user or **Agent** entry in a **Conversation**.
_Avoid_: chat, reply

**Job**:
One unit of work created by a user **Message** and executed by the **Agent**.
_Avoid_: task, run, request

**Claim**:
The first phase of **Job** pickup: the **Daemon** has reserved the **Job**, but it is not running.
_Avoid_: lock, assign, reserve

**Start**:
The second phase of **Job** pickup: the **Daemon** has acknowledged and the **Job** is running.
_Avoid_: begin, accept

**Mode**:
How a **Conversation** displays output (full, brief, result); it does not change execution.
_Avoid_: view, verbosity

**Always-on**:
An **Agent** whose process is kept alive between **Jobs**.
_Avoid_: resident, pinned

### Auth

**Login session**:
The authenticated browser session for the **Hub**.
_Avoid_: session (bare), cookie

**ACP session**:
The conversation state kept by one **Agent**'s Kimi process.
_Avoid_: session (bare), Kimi session

### Files

**Attachment**:
A user file delivered to the **Agent** with a **Job**.
_Avoid_: upload, input file

**Output file**:
A file the **Agent** explicitly sends back into the **Conversation**.
_Avoid_: artifact, result, output (bare)

**Inbox**:
The holding area for **Attachments** that no **Job** has claimed yet.
_Avoid_: uploads, staging

### Skills

**Skill**:
A reusable instruction bundle that teaches an **Agent** a workflow.
_Avoid_: plugin, tool, command

**Skill store**:
The host-local shared library of **Skills**; the source of truth for what can be installed.
_Avoid_: registry, marketplace

**Installed skill**:
A **Skill** made available to one **Agent**.
_Avoid_: enabled skill, bound skill

### Team

**Team**:
A two-level relationship where one **Agent** commands other **Agents**.
_Avoid_: group, swarm, org

**Dispatch**:
A commander **Agent** handing work to a team member, creating a **Job** for that member.
_Avoid_: delegate, assign, task

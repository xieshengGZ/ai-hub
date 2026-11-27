# Output files are explicit sends

An **Agent** returns an **Output file** only by running `aihub-send`. The **Daemon** no longer scans the working directory at the end of a **Job**. Earlier scanning picked up browser caches, temporary scripts, and other noise; explicit sends are predictable and attach the file to the reply the Agent is writing. The cost is that an Agent must remember to send.

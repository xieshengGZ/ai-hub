# A Job is claimed before it starts

A **Daemon**'s poll marks a **Job** as **Claim**ed, but the Job becomes running only after the Daemon posts a **Start** event. A lost poll response therefore cannot leave a Job running forever; the cost is one extra round trip before execution and a re-claim window of about 30 seconds.

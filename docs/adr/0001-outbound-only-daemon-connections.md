# Daemon connections are outbound only

The **Hub** never opens a connection to a **Host**. Every **Daemon** long-polls the Hub for **Jobs** and posts events back over its own outbound HTTPS connection. This lets a **Host** sit behind NAT or a restrictive egress policy, at the cost of the Hub not being able to push work immediately.

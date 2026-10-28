I'm a PS (Professional Services) Engineer working with Vonage Contact Center.

I receive many call flows (IVR), drawn in Visio or DrawIO.

These flows usualy define applet by applet the configuration I have to build in VCC Interaction Plans from VCC (VCC stands for Vonage Contact Center) Admin page.

Usually, I create the flows in a Test environment (a VCC Test account), and later I have to copy the Interaction Plans to PROD (another VCC account).

I would like:
1. A solution which could "read" the flows drawn in Visio/DrawIO and built them into VCC.
2. A solution which could "read" the flows from the Test environment and copy them to PROD.

I work for several customers, so, I need to be able to define which is VCC destination Account (the account defines the environment where I have to work).

a. Do you think is it possible?
b. What do you think is the best way to automate this?
- A script (in python, JavaScript, C#, etc)
- Using Claude Desktop to interact with the browser where Vonage is opened?
- Using Claude Skills/agents?

Once we define what is the best tool for this automation, I think we should build the solution in this order:
1. Create a small POC which created a single applet (for example an announcer) in an Interaction Plan.
2. Build the solution to copy Interaction Plans from one environment to another.
3. Build the solution to translate flows drawn in DrawIO/Visio into VCC Interaction Plans.

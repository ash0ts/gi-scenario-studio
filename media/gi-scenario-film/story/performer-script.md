# GI Scenario Studio — performer script

**Target runtime:** 27:50
**Audience:** a smart first-time viewer who may know nothing about drainage modeling, SWMM, or engineering search.
**Delivery:** warm, curious, and concrete. Explain the human problem before naming the technical idea.
**Pace:** conversational. Pause after questions, comparisons, and short conclusion lines; let the visuals answer with you.

Read only the narration. Headings and bracketed cues are not spoken.

## 00:00:00–00:00:55 · 01 Pareto Trap

Imagine you are responsible for a neighborhood where hard rain can overwhelm the drains. You have 220 improvement plans and no budget to build them all. So you draw a map: cost along the bottom, and how much less water reaches the outlet along the side. One plan looks like a bargain. It costs about 55 thousand dollars and sends a little less water out. But the picture has hidden something. In the long storm, the highest flow rate rises from 3.14 to 3.21 cubic feet per second. A second storm rises too. And the plan still misses our 6 percent reduction target. The plot did not lie. It answered a smaller question. A good decision must ask the rest.

## 00:00:55–00:01:50 · App Home

This is what GI Scenario Studio is for: comparing ways to change how much runoff reaches a drainage system, and how quickly it arrives. Engineers call these features green infrastructure—rain gardens, porous pavement, planted basins, and rain barrels. If you already have a drainage model, you can open it here. For this walkthrough, we will use Cedar Trace, a tiny fictional neighborhood built for teaching. It is not a real town or a design recommendation. It gives us something safer: a complete example we can take apart. The demo stays on this computer; cloud sharing is off. Our question is simple: which plan, if any, deserves the cost of a real field investigation?

## 00:01:50–00:02:50 · 02 Rainfall To Runoff

Before we use any software, follow the water. Rain lands on an area that drains to the same place—engineers call that area a catchment. Some water soaks into soil. Some waits in a shallow basin or rain garden. The rest runs across the surface and into pipes. Eventually it reaches an outfall, simply the point where water leaves the drainage model. The flow there rises and falls during a storm. The total area under this curve is event volume: all the water that passed. The highest point is peak flow: the highest modeled flow rate during the storm. A project can lower one without lowering the other. A different rain pattern can change both. That is why one plan must face more than one storm. These drawings are conceptual, not measured.

## 00:02:50–00:03:50 · 03 Swmm Represents

Now we can introduce SWMM, the EPA's Storm Water Management Model. Think of it as a calculator for a simplified drainage world. The engineer supplies a map of the land, junctions, pipes, and outlet; a rain pattern; and the green-infrastructure plan to test. SWMM follows those declared rules and produces modeled flows and water levels. The app reads the output, checks that the run completed and its numerical balance stayed within the declared limit, and gives each file a digital fingerprint for later. But a careful calculation is still only as good as the world described to it. SWMM cannot see a missing pipe, test the soil, or tell us whether the chosen storm represents reality. Exact calculation is not the same as field truth.

## 00:03:50–00:05:20 · App Guided Overview

Before looking at answers, we write down what good enough means. This matters: if we invent the rules after seeing a favorite, the analysis becomes a story we tell ourselves. Our fictional engineer sets four rules. First, across four practice storms, at least 6 percent less water must reach the model's outlet than in the unchanged model. That unchanged model is our baseline. Second, the 30-year screening cost must stay at or below 200 thousand dollars. Third, no storm is allowed to create a higher peak flow at the outlet. A good total cannot excuse one sharper surge. Fourth, every simulation used in the decision must pass basic numerical checks. The storms and costs are invented for this lesson. And verified replay means the saved files still match the recorded runs. It does not mean the model matches a real neighborhood. With the question and rules fixed, we can look at evidence without moving the goalposts.

## 00:05:20–00:06:00 · 04 Decision Intersection

Picture four lights wired together. Under this teaching brief, a plan advances only when all four are green: enough volume reduction, acceptable cost, no peak increase, and clean runs. ALT-207 turns on all four: more than 6 percent reduction, under 200 thousand dollars, no higher peak, and four clean runs. ALT-017 is cheaper, but two lights stay red. It misses the volume target and raises two storm peaks. We do not average red lights into green ones. One required failure stops the plan.

## 00:06:00–00:07:35 · App Model Preflight

Before comparing plans, the app checks the unchanged model and the saved study. It identifies the exact software version, confirms that every required file is present, opens the model, and confirms that the four stored starting runs completed. The largest water-accounting mismatch is less than one tenth of one percent, comfortably inside this lesson's warning limit. So the saved calculation completed cleanly. One caution remains. No observed storm and no trusted prior result were supplied for comparison. The screen therefore says model fitness: not assessed. That label is not a failure, and it is not permission. It tells us that the saved calculation is inspectable while the model's connection to a real place remains unknown. The acknowledgment simply records that we saw this boundary.

## 00:07:35–00:08:15 · 05 Qa Vs Fitness

A navigation app can follow its map perfectly even when a real road has closed. Drainage models have the same two questions. Did the software run cleanly and account for the water? For Cedar Trace, yes. Does this simplified model describe a real neighborhood well enough for this decision? We do not know. No observed events or approved reference result were supplied. Clean execution earns confidence in the calculation. It does not earn confidence in the world the calculation describes.

## 00:08:15–00:09:50 · App Storms

Rainfall is not just a depth; timing matters. An inch falling gradually can interact with storage very differently from two inches arriving mostly at the beginning. Cedar Trace uses four artificial rain patterns so we can see that difference. One inch over one hour. Two inches over one hour, weighted toward the front. Two and a half inches spread over four hours. And a four-inch, two-hour stress test. These are not historical storms or official design events. They are practice questions. Everything else in the model stays the same while the rain changes, so the comparison is fair inside this example. One surprising lesson will come from the long, four-hour event: it creates ALT-017's worst increase at the outlet even though another storm has a much larger overall peak. The biggest storm is not automatically the one that controls every decision.

## 00:09:50–00:10:40 · 06 Four Responses

Now take one plan and run it through all four rain patterns. We get four answers, not one. For total volume, adding the four storm totals is useful because our teaching rule asks about their combined reduction. For peak flow, adding or averaging would destroy the question we care about: did any single storm get worse? So each peak keeps its own before-and-after comparison. ALT-017 lowers two peaks, but raises two. The long storm is worst, up 0.07 cubic feet per second. If we squash everything into one score, that red warning can disappear. Every summary is a choice about what to forget.

## 00:10:40–00:12:15 · App Opportunities

Now we decide what the software is allowed to change. Not pipe sizes. Not soil properties. Not the design of a rain garden. Only four whole-number counts of additional units at four teaching locations: planted bioretention, areas of permeable pavement, rain gardens, and groups of rain barrels. Each location has a maximum count and a stated drainage destination. The map is a diagram, not surveyed geometry. The engineering details of each practice stay fixed. The search can ask, for example, what happens after adding zero bioretention units, three pavement units, two rain-garden units, and six barrel groups to the unchanged model? It cannot invent a new soil mix or pretend a utility conflict does not exist. Automation is useful when it explores choices an engineer has deliberately admitted. Here, demo approved means allowed into the lesson—not feasible, constructible, or approved for a real site.

## 00:12:15–00:13:30 · 07 Bounded Search Space

Those four additional-unit counts work like four dials. The first has five settings, the second four, the third five, and the fourth seven. Multiply them and we get 700 possible combinations, including the unchanged baseline. Testing every combination under four storms would require 2,800 SWMM runs. That complete study exists as a benchmark. The guided story is smaller: 220 chosen combinations, four storms each, plus four baseline runs—884 runs in all. It is not every possibility, and it is not a demonstration that machine learning found a better answer. Imagine the untested combinations as face-down cards. A search method may choose which card to turn over next. That choice is a question, not an answer. Only a direct SWMM run can reveal the modeled hydraulics. Search decides where to look. SWMM supplies what we see.

## 00:13:30–00:14:50 · App Goals

The four plain-language rules now become checks the computer can apply consistently. Water volume is measured at the same outlet and compared with the same unchanged model. Peak flow is checked separately for every storm. The teaching cost rolls installation, future upkeep, and replacement over 30 years into one today-dollar screening estimate. It keeps the lesson consistent; it is not a contractor's price or a local budget. Every option also needs four completed SWMM runs and clean numerical checks. If this tiny model reports zero for a flooding measure, that remains a modeled result—not a promise that a real street stays dry. The point of this screen is not to add more numbers. It is to stop the meaning of success from changing after an attractive plan appears.

## 00:14:50–00:15:40 · 08 Job Matrix

Here is where 884 comes from. Put the four storms across the top. The unchanged model fills the first four squares. Then each of 220 plans gets a row of four more squares—one real SWMM run per storm. Four plus 220 times four equals 884. Each square keeps the result, the numerical check, and digital fingerprints of its files. The project also keeps complete input, report, and output files for the four baseline runs and for four teaching plans under four storms: 20 full evidence sets. Opening those finished records is a replay. Running all 884 again would be a new experiment. A replay is a receipt, not a progress bar.

## 00:15:40–00:17:10 · App Run Verified Replay

This screen is showing receipts, not pretending to run a simulation. The 884 jobs were completed earlier and stored in a manifest—a list of what ran and which files belong to each result. It names the fixed SWMM version, the 220 plans, the four storms, and the numerical check for every job. The plans were ordered in a repeatable way chosen in advance; machine learning did not select this guided set. A local rerun would be different: the app would launch SWMM again, show real progress, and compare the new answers with the stored ones. That control appears only when the expected engine is actually available. It is not available in this recording, so the app does not fake it. Replay lets us inspect what happened. Rerun lets us test whether it happens again. Neither should be confused with work currently running.

## 00:17:10–00:19:20 · App Compare Acceptance Matrix

Now the plans become characters rather than dots. First is the bargain-looking plan, ALT-017: about 55 thousand dollars and just over 4 percent less total water. That misses our target, and its long-storm peak rises from 3.14 to 3.21 cubic feet per second. Cheap is not the same as acceptable. Next is ALT-207: about 6.4 percent less water for 190 thousand dollars. None of its four peaks rises, and every run passes its checks. It clears this teaching screen, so it is a candidate to carry forward for site and feasibility review—a question to investigate, not an approval. ALT-109 offers about 7 percent reduction, but its 222-thousand-dollar cost crosses the cap. ALT-014 gives the strongest volume reduction seen among all 220 guided plans, about 8.8 percent, but costs roughly 362 thousand dollars. More hydraulic benefit can still be the wrong fit for the brief. The detailed values remain on screen, while the app evaluates the unrounded stored numbers. Among these four examples, one clears every rule. We have earned a smaller set of questions, not a design verdict.

## 00:19:20–00:20:20 · 09 Hidden Dimension

Why did ALT-017 look so good at the beginning? Because a flat plot is a shadow. Here, better means farther left for lower cost and higher for more volume reduction. Along the visible edge, no other tested plan is at least as cheap and at least as effective while improving one of those measures. Engineers call a point like that nondominated, and the edge a Pareto frontier. The names sound like verdicts. They only describe this two-number picture. Bring back the 6 percent target and ALT-017 falls short. Bring back each storm's peak and two red increases appear. Then remember that the model's real-world fitness is still unknown. A Pareto frontier is a useful map of one tradeoff. It is not the border between acceptable and unacceptable.

## 00:20:20–00:21:50 · App Compare Supporting Views

Now the plot has a proper job: exploration. The teal edge shows the cost-versus-volume tradeoff we just named. Each gray dot has a tested competitor that is at least as cheap and effective, and better on one of those measures. The ochre dots are our four teaching plans with complete files ready to inspect. Select ALT-017 beside ALT-207. The first is cheaper. The second clears the full brief. Neither fact cancels the other. We can change the vertical measure, open the four storm results, or read the accessible table instead of the chart. Each view opens another window onto the same stored simulations; none creates a new run. Use the plot to decide where to look. Use the declared rules to decide what passes this screen.

## 00:21:50–00:22:50 · 10 Evidence Chain

Take one red number: plus 0.07 cubic feet per second. Imagine handing it to another engineer. Their first question should be, compared with what? The answer is the long storm: 3.14 in the unchanged model, 3.21 in ALT-017. Then comes the receipt. This plan added one rain-garden unit and six rain-barrel units. The project stores that compactly as zero, zero, one, six. It wrote the change into an input file, ran the fixed SWMM version, kept the report and detailed output, identified the files with digital fingerprints, and recorded the numerical check. That chain is called provenance: a path from a claim back to what created it. It shows how the number was made. It does not prove the model matches the real world.

## 00:22:50–00:24:40 · App Evidence Rail

Inside the app, open ALT-017 and ask for the built-in explanation. It does something deliberately unglamorous: it copies both recorded peak failures from local evidence. The long event rises from 3.14 to 3.21 cubic feet per second; the stress event rises from 13.33 to 13.34. These are not values remembered by a language model. Just below them, the model-backed Evidence Guide is held behind its release gate. The evaluation selected no model, so the interface makes no inference call and substitutes no plausible-sounding answer. That is what fail closed means: the optional explanation disappears, while the evidence stays useful. Now move to ALT-207. It passes the four synthetic teaching rules, but hydrology is not ecology. These runs do not measure water quality, groundwater recharge, habitat, heat, actual flooding, maintenance, or lifecycle effects. The rail keeps what the model supports beside what remains unknown. Computation can organize evidence; it cannot enlarge it.

## 00:24:40–00:26:15 · App Export And Observability

The final product is not a winning dot. It is a handoff another engineer can question. The package carries the original question, the rules, the plans compared, the results, the model runs behind them, and the limitations that still matter. The app checks those pieces before enabling the learning export, then records which cases include complete input, report, and detailed output files. For this recording, we download a compact local summary. W&B can mirror the evidence for private collaboration only after the destination's access policy is verified. It is not verified here, so nothing uploads. The local receipt remains available. A good export does not make the conclusion larger. It makes the work easier for the next person to inspect.

## 00:26:15–00:26:55 · 11 Engineer Decides

ALT-207 passes the teaching screen. Now the question moves outside the computer. Is there space? Where are the utilities? Will the soil soak up water? Who maintains it? Can it be built? Are the storms, costs, and model appropriate? Good screening does not manufacture certainty. It focuses expensive field work. ALT-207 is only a candidate to carry forward for site and feasibility review. SWMM computes. Search allocates attention. Evidence preserves the path. The engineer decides.

## 00:26:55–00:27:50 · App Closing Export Home Or Decision Summary

The product's value is not an automatic winner. It is the ability to say: here are the plans, the rules we chose first, the reason each example passed or failed, and the model runs behind those statements. A practitioner can repeat this with an existing SWMM project, locally appropriate storm definitions, engineer-predefined locations, and local costs. The result will still depend on that model and its assumptions. But it is no longer a mysterious dot. It is a claim another engineer can open, question, and trace. That is the difference between trust this result and here is how to check it.

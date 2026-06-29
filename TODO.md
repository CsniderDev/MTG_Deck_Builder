# TODO

- Save current searches to local storage to ensure data is persisted in the tab
- Ensure mana production mirrors mana costs. right now it seems to be just leveraging an even spread mana base where it needs to better 
  match what the costs are.
- If a revamp is done, persist the previous decklist and dont default to the heuristic build
- Add security measures in place to ensure malicious users dont overload APIs or spam requests to the LLM


### 1. "Salt Score" & Power Level Calibration

Commander players are obsessed with matching the power level of their local playgroups.

* **The AI Feature:** Have the LLM analyze the finalized 100-card list and provide a "Power Level Narrative" (e.g., Casual, Focused, Optimized, or High-Power/cEDH).
* **The API Synergy:** You can have your backend cross-reference EDHREC's salt data or check the deck list against known high-power combos (like Thoracle/Consultation or dramatic scepter lines) and have the AI explain *why* the deck sits at that power level.

### 2. Smart Mana Base & Land Tagging

Building a mana base is usually the most tedious part of deck building.

* **The AI Feature:** A "Generate Smart Mana Base" button. Instead of just adding generic basics, the AI looks at the mana pie chart, calculates the exact colored pip requirements, and automatically suggests a tailored land suite (including utility lands, duals, and fetch targets) matching a user-specified budget slider.

### 3. Contextual "What's My Win-Con?" Summaries

A common issue with AI-generated or heavily edited decks is that they can become a pile of good cards that lack a cohesive way to actually end the game.

* **The AI Feature:** A dedicated tab that clearly lists the deck’s primary and secondary win conditions. For example:
> **Primary:** Combat damage via Go-Wide tokens token-pump (creates ~4 distinct paths via *X* and *Y* cards).
> **Secondary:** Infinite drain combo using *Card A* + *Card B*.



### 4. Interactive "Goldfish" Simulator with AI Coaching

"Goldfishing" (playing the deck solo to see how fast it sets up) is how players test their decks.

* **The AI Feature:** A lightweight sandbox where the user can draw an opening hand of 7 cards. The AI analyzes the hand and says: *"Keep this. You have a turn 2 ramp piece and 3 lands,"* or *"Mulligan. You have three 5-drops and no early color fixing."* ### 5. Tag-Based Deck Exploration (Custom Categories)
Instead of standard sorting (Creatures, Sorceries, Lands), use the AI to auto-tag cards by their *functional* role in Commander.
* **The AI Feature:** The app automatically breaks the deck down into custom categories like **[Draw Engines]**, **[Targeted Removal]**, **[Board Wipes]**, and **[Ramp]**. If a deck is running only 4 pieces of interaction, the AI can visually flag that category in red and say, *"Hey, you're a bit light on removal for a standard pod."*

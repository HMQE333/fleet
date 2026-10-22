# Attribution

`fleet/fastbrowser` adapts code and ideas from two MIT-licensed projects.

## jev-ultrafast — https://github.com/browser-use/jev-ultrafast
Copyright (c) 2026 Browser Use. MIT License.

Used: `snapshot.js` (page reader, adapted), the observe → choose → act loop with decisions consumed once,
freshness checks right before input and hit-tested CDP input (`page.py`, `engine.py`), the operation/target
question structure and answer validation (`decide.py`), and the text-helper prompt.

## JevBrowser — https://github.com/digitalfoudnry-vb/JevBrowser
Copyright (c) 2026 Joey Kudish. MIT License.

Used: calling Jev through OpenRouter's decisions API, action-space noise filtering and link de-duplication,
the `goal_done` / `stuck` watchers, the `back` action and next-best-action recovery (`decide.py`,
`engine.py`), and the public-address network guard (`netguard.py`).

## MIT License (applies to the adapted portions)

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions
of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

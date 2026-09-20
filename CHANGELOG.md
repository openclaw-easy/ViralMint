# Changelog

All notable changes to ViralMint will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Bring your own images into a Smart Video.** The studio could take one
  picture, and only as an all-or-nothing swap: it became the entire video and
  no stock footage appeared at all. There is now a **Your Images** panel in the
  studio rail — add as many photos as you like and each one fills a scene, in
  the order you added them, starting with the hook. Every scene you didn't
  cover still gets stock footage matched to that line of the script. Each photo
  is animated with a slow zoom or pan rather than sitting still, and it is
  fitted to the video's shape rather than stretched into it. Bringing more
  photos than the script has scenes for makes the video cut more often so they
  all fit; past twelve, you're told which ones couldn't be placed instead of
  finding them missing later. If a photo turns out to be unreadable, or has
  gone missing since you picked it, that scene falls back to stock footage and
  says so.
- **A cutting bench in Clip Studio.** Selecting a source video used to blank
  the centre of the page — it only drew something once a *clip* was selected,
  so picking a video with no clips yet, which is when you most need a
  workspace, said "Select a clip to preview". And cutting by hand meant typing
  timestamps into a box, choosing moments in a video without seeing a frame of
  it. There is now a timeline you drag on: a real filmstrip of the source's
  frames, your pending cuts drawn on top of it, a lane showing where the
  talking is, faded bands for clips you already cut from this video, and first
  and last frame previews that follow the handles as you move them. Handles
  snap to sentence boundaries so a clip doesn't open mid-word (hold Alt to
  move freely). Press play and it plays *that clip* — starting at its in
  point, stopping at its out point.
- **Four ways to place a cut, one list.** Drag across the filmstrip, press N
  at the playhead, paste times you already have ("0:42-1:05, 2:10 → 2:38",
  from show notes or a chapter list), or ask the AI. All four produce the same
  editable block. Every pending cut is a row beside the video with **typeable
  timecodes** — dragging is right for finding a moment and wrong for "start it
  at exactly 1:30", and a typed time moves the block on the timeline just as
  if you had dragged it there.
- **The AI proposes clips instead of just cutting them.** "Ask AI" reads the
  transcript and puts its picks on the timeline as ordinary blocks, labelled
  with its own reason and hook score, which you can nudge, retime or delete
  before anything is rendered. Its choice of in and out point used to be final
  and invisible until the finished clips appeared.
- **Auto-cut says what it will do.** The old dialog is now a single-purpose
  express lane — trust the AI, cut immediately, any number of clips — and it
  tells you the number first. Leaving "Clips (max)" blank means about one clip
  per 30 seconds, so a 22-minute podcast quietly meant 43 renders; the button
  reads "Cut now · up to 43 clips" now, from the same rule the renderer uses.
- **Captions, emoji, silence trimming and vertical framing are set once.**
  They apply per clip whichever way you found it, and they used to be
  duplicated across the two surfaces — so a caption style picked in one place
  silently failed to apply in the other, and whichever screen you happened to
  finish on decided the result.
- **The Library shows everything you own.** Every tool wrote its output to disk
  and none of it appeared anywhere: captions, reframes, merges, trims, crops,
  GIFs, subtitle files, chapter lists — twenty tools, invisible the moment the
  job finished, reachable only if you remembered where the download button put
  the file. The Library is now one faceted view over four stores at once (your
  renders, your downloads, every tool output, and the background-music
  directory).
- **Two questions, two controls.** The page used to tab on a mix of them:
  Scout / Downloaded / Generated asks where a file came FROM, while asking
  whether something is audio asks what it IS. Anything with a true answer to
  both — a downloaded mp3, a voice-over, an enhanced podcast — had to be filed
  under one and was lost from the other. Media tabs now say what a file is and
  "From" chips say where it came from, so nothing has to choose. Colour means
  provenance and nothing else, and every tile states its origin in words too.
- **By source.** A second view groups each download with everything you made
  from it — the clips you cut, the captioned version of one of those clips —
  so a file and its descendants are finally visible together.
- **Activity, from any page.** The job log was a Library tab, so the only way
  to answer "is my clip still rendering?" was to navigate away from whatever
  you were doing. It is now a panel any page can open, with a running-jobs
  button in the sidebar. Work in flight shows as a tile in the grid where its
  output will land.
- **Scout has its own page.** A trending video is a lead, not a file you own —
  no bytes on disk, nothing to play or edit — and it grows fast enough to dwarf
  the library it was filed inside. Old `?tab=` links still work and are
  translated into the new filters on arrival.
- **Posters for edited videos.** Nothing in the tool pipeline ever made a
  thumbnail, so a captioned cut had no image to show. One is extracted on first
  view and cached; audio and text draw a waveform or a snippet rather than a
  grey block.

### Fixed
- **Captions are burned on Windows.** They never were. A path with a drive
  letter in it broke the filter ViralMint hands to FFmpeg, so every caption
  burn on Windows failed — Clip Studio clips, the Captions tool, Smart Video —
  and the app reported success anyway, because a burn that produced nothing
  still looked like one that worked. Paths are now quoted the one way that
  survives FFmpeg's two parsing passes, an empty burn is treated as the failure
  it is, and the log carries FFmpeg's actual error instead of its version
  banner. If clips do get saved without the captions you asked for, you are
  told.
- **Auto-zoom does something.** The zoom pulse filter had never once been
  accepted by FFmpeg — the expression was built in a form FFmpeg splits apart,
  and it animated a value FFmpeg only reads once. Every auto-zoom in the app's
  history quietly handed back a copy of the source and called it a success. The
  filter is rebuilt, a failure is now a failure instead of a silent copy, and
  Smart Video tells you when captions, zoom or the watermark could not be
  applied rather than shipping the video as if they had been.
- **Cancel stops the work.** Cancelling a download used to do nothing visible:
  the batch downloaded every remaining video, then wrote "success" over your
  "cancelled" and announced it had finished. The same was true of every tool —
  a cancelled crop, trim or compress ran to completion and reported success.
  All of them now stop at the next safe point. Whatever had already started
  downloading still finishes and is kept — losing a file you already have is
  worse — but nothing after it runs, no transcription follows, and the job
  stays cancelled. A cancel is also no longer reported as a failure.
- **Overlapping cuts can't stack any more.** Dragging one pending cut onto its
  neighbour, or typing an overlapping time into the rail, quietly produced two
  near-identical clips from the same seconds. A cut now parks against its
  neighbour instead of sliding over it, the server refuses overlapping ranges
  whatever sent them, and cutting clears the bench so the button can't fire
  twice over work you already have.
- **The Clipper stops freezing the app on a big extract.** Measuring 50 finished
  clips ran unbounded, and every FFmpeg operation in ViralMint shares one pool —
  so a large extract starved the bench's own filmstrip and anything else
  running. A second bug compounded it: the save step measured clips while
  holding the database lock, so other work got "database is locked". Both are
  fixed.
- **Long clips finish.** Clip extraction allowed ten minutes regardless of the
  clip's length, so pulling a long section out of a long recording hit the wall
  on every range and then blamed the source video for being corrupt. The budget
  now scales with what you asked for.
- **A cancelled or interrupted upload no longer leaks gigabytes.** Navigating
  away mid-upload left the partial file on disk forever, referenced by nothing
  and found by no cleanup.
- **An interrupted aspect-ratio conversion no longer poisons the result.** A
  timed-out export left a truncated file exactly where the finished one belongs,
  and every later export handed that back instantly. Conversions now land
  atomically, are checked before use, and are encoded in a format Safari and iOS
  will play.
- **The first run says it is downloading the speech model.** Whisper's model
  files aren't in the installer, so the first transcription downloads 150 MB to
  3 GB — and almost nothing said so. Worse, the download ran on the main thread,
  which froze the entire app, progress bars included, until it finished. It now
  runs in the background, tells you it is happening, and fails with an
  explanation you can act on instead of a stack trace.
- **A sleeping external drive can't delete your library.** If ViralMint's data
  lives on a drive that is asleep or unplugged, every file looks missing. The
  cleanup routines took that at face value and would have deleted the rows
  behind your clips, transcripts and analyses while the files sat safely on a
  drive that came back a minute later. They now do nothing until the drive is
  reachable.
- **Uploaded cookies are no longer downloadable.** The endpoint that serves
  images you upload would serve any file in its scratch folder by name,
  including the browser session used for downloads. It serves images only now.
- **Nothing else on your machine can drive the chat.** The chat connection
  accepted a browser page from any website, which could then run jobs, cancel
  yours, and read everything the app sent back. It now only accepts the app's
  own pages — and, as before, non-browser clients such as the messaging bridges.
- **A drastic silence trim tells you.** Removing silence from a clip that is
  mostly music or room tone could return a fraction of its length with nothing
  but a log line to show for it.
- **AI-written caption styles can't corrupt the render.** A style generated with
  a font list or a web colour code shifted every following setting, so captions
  came out unstyled or invisible while the job reported success.
- **Failures say what went wrong.** FFmpeg prints its version banner before any
  error, and ViralMint was logging the banner and discarding the error — so a
  failed render, a dropped colour grade or an unplayable clip left a log entry
  that said nothing at all. Every one of those now carries the real message.
- **Cancelling an AI clip search now actually stops it.** Ask AI on the
  cutting bench runs Whisper and then the model; nothing interrupted that work
  when you pressed cancel, so it carried on, made the model call anyway, and
  then wrote "success" over your "cancelled" — and the bench adopted proposals
  you had cancelled. The search now checks for a cancel after the transcript
  and again before adopting the result, and a cancelled search ends quietly
  instead of as a failure.
- **A refused AI call no longer turns into random clips.** When the model
  could not be called at all — a bad or missing API key, a revoked model, a
  rate limit — the picker used to treat that like "the AI found nothing" and
  fall back to slicing the video into evenly spaced windows, which then
  showed up labelled as viral picks with no word about why. Those refusals
  now fail the job with the real reason; the fallback is only for a genuine
  no-result.
- **Dragging a cut across a pending one no longer stacks a second cut on top
  of it.** The N key, paste and AI adoption already refused overlaps;
  drag-create was the one door left open, and manual cutting cuts ranges
  verbatim, so a drag that swept over an existing block produced two
  near-identical clips. The drag now clamps into the free gap, the preview
  while you drag shows exactly what will land, and a drag with less than a
  second of room says so instead of silently doing nothing.
- **Clips are drawn at their real shape.** The clip card's thumbnail was a
  fixed portrait frame, and the inspector's shape chip said "9:16" whatever
  the clip was; a clip cut from a square or portrait source (which keeps its
  own shape) was centre-cropped to a sliver. Both now follow the shape probed
  from the finished file, and a probe that fails under load re-probes the
  file instead of guessing portrait.
- **The inspector's Transcript section can render.** The clip list omits the
  transcript text to keep the page light, and the inspector never fetched it,
  so the section existed but never appeared. It now loads once per selected
  clip.
- **Regenerating a thumbnail shows the new image.** The regenerated file had
  the same URL as the old one, so the browser kept showing what it had. The
  URL now carries a version.
- **Ask AI survives leaving the bench.** Peeking at a finished clip while a
  search ran silently orphaned it — the proposals were never adopted. The
  running search is remembered per source and re-attached when you come back,
  and a finished search refreshes the source so the next cut does not re-run
  the Whisper pass the search just did.
- **Long podcasts snap all the way through.** The bench's speech lane asked
  for the default 1,500 segments, so snapping stopped working past that point
  on a long source. It now asks for the full transcript.
- **Cut submits no longer fail on a stale range.** Ranges restored from a
  previous visit are clamped to the source's current length; before, one
  block past the end rejected the entire cut.
- **Ranges the model or a chapter list handed over with a NaN bound are
  rejected up front** with a clear message, instead of reaching ffmpeg as
  `-ss nan` and failing the whole job opaquely. The bench's frame preview
  likewise rejects an infinite time instead of crashing.
- **A page load with the storage drive unmounted no longer deletes your video
  library.** The Library's self-heal removes rows whose file is gone; with the
  whole storage volume absent every file looked gone, so one page load could
  delete every row and its sibling files. The prune now stands down until
  storage is back.
- **Cached preview images can no longer be served half-written.** The bench's
  filmstrip and frame previews, and every generated thumbnail, are now written
  to a temporary file and moved into place, and an empty frame is refused, so
  a concurrent request can never pin a truncated image. Two simultaneous
  requests for the same filmstrip also build it once instead of twice.
- **A video with no readable duration is no longer labelled portrait.** The
  media probe read width, height and duration all-or-nothing, so a file
  whose container carries no duration threw away its perfectly good
  dimensions — and a 1920x1080 clip was then treated as 9:16 by the caption
  layout and the Library tile. Each field is now read on its own.
- **Dragging on the bench is smooth with a full clip rail.** Every mouse move
  used to re-render the whole page, including every clip card; the derived
  lists are now cached and the cards only re-render when their own clip
  changes.
- **The voice pickers offered voices that could not work.** Every voice you
  could choose for a translated dub was a name the dubbing engine has never
  had, so full dub failed whichever one you picked. The Voice-over tool
  offered the same names under "OpenAI TTS", which OpenAI also rejects, and
  its provider switch was decorative — choosing OpenAI handed the free engine
  a name it didn't know and the job just failed. Both pages now offer what the
  engine they call actually accepts, the Voice-over tool really does narrate
  with OpenAI when you pick it and your key is set, and picking it *without* a
  key narrates with the free voice and tells you that's what happened instead
  of failing. Translate also offers all ten caption styles rather than three.
- **Voice previews never played.** The little play button beside each voice
  asked the app for a sample and the app had no way to answer, so it always
  said "Preview failed". It now plays a real ~7-second sample, with different
  wording each time so you hear how a voice handles more than one sentence.
  The voice lists themselves are also served live now, rather than the pages
  falling back to a short built-in list on every single load.
- **A video that couldn't be read said the timeline was fine.** A download
  interrupted part-way leaves a file that looks valid and contains no usable
  picture. Clip Studio showed an empty timeline with "the timeline still
  works", which was untrue — playback, the frame previews and the cut itself
  would all fail too. It now says the video has no readable frames and that it
  may be an incomplete or cancelled download. Behind that, such a file used to
  cost dozens of failed decode attempts *every time you selected it*; now it
  costs two. And when something ffmpeg does fails, the log finally says why
  instead of printing ffmpeg's version banner.
- **Expired thumbnails left broken cards in Scout.** Some platforms sign their
  thumbnail links so they stop working after a while, so results from an
  earlier scout showed a broken-image icon with the play button floating over
  nothing. Those cards now show a quiet placeholder and keep everything that
  is still good — title, stats and buttons. One dead thumbnail no longer
  affects its neighbours.
- **Two colours were too faint to read.** The "CREATED" label in the Library
  and the green status chips in Scout both sat below the readable-contrast
  threshold. Both are now readable, and the Library's colour rail keeps the
  brand colour it always had.
- **Photos with a transparent background rendered as garbage.** Any image
  taken into a video had its transparency *discarded* rather than filled in,
  which left whatever colour happened to be hiding underneath — so a cut-out
  subject came out surrounded by a fringe of it, in a file with nothing
  otherwise wrong. Transparency is now flattened onto black first. The same
  pass takes the first frame of an animated GIF instead of failing on it, and
  scales a camera-sized photo down before the renderer has to hold all of it
  in memory.
- **Photos were stretched, and slow pans stopped halfway.** A picture whose
  shape didn't match the video's — a square photo in a vertical short, a
  panorama in a widescreen one — came out visibly squeezed or elongated. And a
  panning shot spent half its time as a still frame before lurching across,
  because the pan was told to travel further than the renderer would allow.
  Both are fixed, and every image-to-video path in the app shares one
  implementation of the movement now.
- **Video scrubbing.** No media route honoured the `Range` header, so dragging
  through a long podcast made the browser re-fetch far more than it needed and,
  depending on the version of the server library underneath, could hand back
  the wrong bytes entirely for the request a player makes to find a file's
  index. Every player in the app — the bench, the Library, clip previews, tool
  results — now goes through one implementation that gets it right, and an
  open-ended request is answered in bounded windows instead of streaming a
  150 MB file to a browser that only wanted to move the playhead.
- **A short video produced no timeline at all.** A file's reported length runs
  to the end of its final frame, so sampling "the middle of the last cell"
  asked for a frame that doesn't exist — and one missing frame threw the whole
  filmstrip away. A six-second clip asked for 32 frames got nothing.
- **The same video was transcribed twice.** Two clip jobs started on one source
  moments apart each ran Whisper over the same audio: the second waited for the
  first to finish, then redid its work from scratch, because it was still
  holding the "no transcript yet" answer it read before it started. On a
  14-minute source that was nearly three minutes of work done twice for
  nothing.
- **One bad suggestion no longer loses all of them.** A clip window that came
  back from the model missing a start or end time raised an error that failed
  the entire search rather than dropping that one pick.
- **The bench's cached frames are cleaned up.** Editing writes hundreds of
  small preview images, and nothing ever removed one — not when the source was
  deleted, and not on any schedule. Deleting a video now takes its cached
  frames with it, and anything older than a month is swept at startup.
- **Clearing the activity log no longer deletes your files.** A successful tool
  run is now the Library item for the file it produced, which made every path
  that removed a job row a path that could make a video disappear while its
  bytes sat on disk forever. Removing such a row is refused with a message
  pointing at the door that removes the file too, and "Clear finished" keeps
  those rows and says how many it kept.
- **The jobs and scout tables are bounded.** Neither ever had a row removed.
  Jobs are now kept for 30 days (2,000 row ceiling) and scout results for 60
  (5,000), swept in the background at startup. Both are defined by what a row
  IS rather than its age: a job backing a file on disk is kept at any age, and
  a scout lead a download still points back at is kept because that is where
  the source URL lives.
- **Relative times were wrong by your UTC offset.** Timestamps are stored as
  naive UTC and were being read as local, so at UTC+2 a job that started five
  minutes ago read as "2h ago".

### Security
- **Batch download refuses non-http links.** `POST /api/downloaded/batch-download`
  accepted any string as a URL and handed it to yt-dlp, so a `file:///etc/passwd`
  entry started a job. Every URL must now be a full `http(s)` link, checked at
  the endpoint itself rather than at one of its callers.
- **Subtitle language codes are shape-checked.** yt-dlp treats the subtitle
  language list as regexes, so a `.*` would pull every track a video has — the
  same fan-out the explicit "all" rejection exists to prevent. Entries must now
  look like real language codes.
- **SPA path-traversal fix.** The frontend catch-all route served any file
  resolved under the `dist` directory without a containment check, so a
  `../`-laden request could read files outside the built bundle. The handler
  now confirms the resolved path stays inside the bundle before serving.
- **CSRF origin check.** Non-safe-method requests must now carry an
  allowlisted `Origin`/`Referer` (no-Origin CLI/non-browser calls still pass),
  hardening the loopback surface against a malicious page POSTing to
  `127.0.0.1:16888`. Skipped when you opt into `HOST=0.0.0.0` LAN mode.
- **Encryption-key validation.** A placeholder or malformed `ENCRYPTION_KEY`
  used to slip through and crash every encrypt/decrypt at first use; it's now
  validated (and regenerated if invalid) at startup.

### Changed
- **Dependencies refreshed, and one dropped for security.** FastAPI, uvicorn,
  SQLAlchemy, Alembic, cryptography and the Anthropic and OpenAI SDKs were all
  behind — the two AI SDKs and cryptography by a full major version or more,
  because their version ceilings had never been raised. `python-jose` was
  removed outright: nothing used it, and it was the only thing pulling in a
  package with a published timing-attack advisory and no fixed release. A
  fresh install now reports no known vulnerabilities. `moviepy` was also
  removed — nothing imported it either, and the piece that actually mattered
  (the bundled ffmpeg used by the packaged app) is now a dependency in its own
  right rather than something inherited by accident.

- **Fewer redundant requests on every page change.** Opening a page asked the
  app for the job list four times over; it now asks twice, and restoring
  in-progress work takes one request instead of two.
- **Chat replies render smoothly.** Every streamed token wrote to the store and
  re-parsed the whole partial reply, so long answers got progressively jankier.
  Tokens are now batched to at most one update per frame — same output, a
  fraction of the work.
- **Background job polling backs off.** The jobs list was re-fetched every 5s
  forever, including in hidden tabs. It now pauses while the tab is hidden,
  slows to 30s when nothing is running, skips redundant re-renders when the
  payload hasn't changed, and catches up immediately when you return to the tab.
- **Faster startup.** Every boot fired an `ALTER TABLE … ADD COLUMN` for each
  migrated column and swallowed the resulting "duplicate column" error — a pile
  of throwaway failed statements on an already-current database. Startup now
  reads each table's columns once and skips those ALTERs entirely.

### Added
- **Motion Graphics — render designed video, locally.** A new kind of output
  alongside stock footage and clips: motion-graphics pieces built out of type,
  shapes and animation rather than filmed footage. Kinetic typography hooks,
  stat cards, lower thirds — the sort of thing that normally means opening After
  Effects.

  It renders entirely on your machine. There is no model in the loop, so the
  same inputs give the same video every time, and you can render it a hundred
  times without spending anything.

  The engine is an optional add-on rather than part of the download. Open
  Settings → Add-ons and install it once: ViralMint fetches a portable Node
  runtime and the HyperFrames engine into your data folder, verifies the
  archive against the checksums nodejs.org publishes, and then renders a real
  video before it will call the install good — so a broken setup fails at
  install time instead of the first time you need it. "Remove" puts the disk
  back. A "Test render" button re-proves the whole chain whenever you want it.

  Because compositions are GSAP-driven and GSAP ships under a licence that is
  not free software, it is not committed to this repository. It is installed
  alongside the engine on your machine and copied into each render instead.

- **The Motion Graphics studio.** A new page in the sidebar embeds a full
  compositing studio: timeline, live preview, layers and variables, an asset
  library and Export. Design a piece, scrub it, adjust it, render it.

  It runs on your machine and is served from the app's own address, so it keeps
  its state between visits, and it is re-skinned to match whichever theme you
  are using.

  Exports land in your Library on their own — the page notices new renders
  while it is open and imports them, so a finished piece is in the same place
  as everything else the app makes, ready to caption, reframe or export like
  any other video. Every composition you replace is archived rather than
  overwritten, and a Comps list lets you clear the ones you no longer want.

- **AI Compose.** Describe the video you want and your own AI model writes the
  composition, which opens in the studio ready to edit. Attach an image, video
  or audio file and it gets built in.

  It uses whichever provider and model you configured, so quality follows your
  choice rather than ours, and nothing about your brief leaves your machine
  except the request to your own provider.

  A composition that would not render is not handed to you broken. It is
  checked against the renderer's contract — both a fast structural pass and the
  engine's own verification — and anything wrong is fed back to the model to
  fix. If it still cannot be made to render, the compose fails and the
  composition you already had is left exactly as it was, rather than being
  replaced by something that does not work.

- **Video Download.** A new tool page for the thing the app could always do but
  never let you choose about: paste up to 20 links and pull the videos down
  from YouTube, TikTok, Bilibili, X and the 1,800+ other sites yt-dlp supports.
  Pick a maximum resolution, keep subtitles as a separate `.srt` or embed them
  as a selectable track, choose MP4 or MKV, and optionally embed cover art,
  tags and chapter markers. Every option degrades instead of failing — a source
  with nothing at the size you asked for gives you the closest available rather
  than an error, and a container that the codecs can't stream-copy into comes
  out as MKV instead of a failed merge. The result then tells you what you
  actually got: the delivered resolution and container per video, which
  subtitle files were kept, and whether the embed extras had to be dropped.
  Unlike the other download paths this one skips transcription — it was asked
  for the files, and Library can analyze on demand.
- **Compress Video.** A new tool for getting a file under an email, chat or
  upload limit. Two independent dials — a target resolution and how hard to
  squeeze at that size — with the output dimensions stated before you run it.
  Asking for a resolution larger than the source keeps the source size instead
  of upscaling, and a file that comes out bigger than it went in says so rather
  than being handed back silently. Local FFmpeg; nothing leaves your machine.
- **Crop Video.** Drag a box over the frame and keep just that part — the
  manual counterpart to Reframe, which picks the framing for you. Free-form or
  snapped to 9:16, 1:1 or 16:9, with the exact pixel crop shown as you drag.
  The audio track is copied across untouched.
- **Remove Audio.** Strip a video's sound entirely, as its own operation on the
  Transform tool and its own card in the Tools hub. This is not the old "Mute"
  volume preset, which re-encoded the audio to silence and left the (silent)
  track in the file; this removes the track. A video that never had audio still
  gets its file back, with a note explaining why nothing appears to have
  changed.
- **A live preview of the file you're about to process.** Selecting a video on
  any tool page now mounts the actual video instead of showing a filename and a
  byte count, and the tool's own controls can read its real size — which is
  what makes the crop box possible.
- **A job can no longer report success on a broken artifact.** Every
  file-producing tool now passes its output through one gate before the job is
  marked successful: the file must exist, be non-empty, be readable by ffprobe,
  carry the kind of stream its own extension promises, and have a duration.
  Tools that know more assert more — reframe now declares that its output must
  be portrait, which is the class of failure an exit code cannot see. Broken
  output fails the job with a sentence naming what's wrong instead of handing
  you a download that doesn't play.

### Fixed
- **Deleting a downloaded video sweeps its subtitle files.** Subtitles kept
  alongside a video have no database record of their own, so they used to
  survive the row that owned them and accumulate on disk forever. Delete and
  the stale-record cleanup both sweep them now.
- **Sound effects no longer make the voice ramp up in volume.** The mixer let
  FFmpeg average the tracks instead of summing them, and every effect counted
  as "playing" from the very start of the video — so the narration began
  roughly ten times too quiet and grew louder as each effect finished.
  Measured on a 20-second clip with 8 effects: it climbed 16.6 dB from start to
  end. It now holds a steady level, with a limiter so the mix can't clip.
- **The "heavy" sound-effect style stopped dropping a third of its effects.**
  It planned up to 25 and the mixer quietly stopped at 15, while still
  reporting the planned number back to you.
- **A news article that only has a headline is now scored instead of
  discarded.** Google News links go through a redirector whose target often
  can't be fetched, leaving a title and no body — which the analyzer treated as
  an empty article and dropped. On some searches that was most of the results.
- **Chat messages no longer arrive twice.** Under some mounting orders the app
  opened a second WebSocket without closing the first, and both delivered every
  server event to the same handlers — duplicate replies, doubled streaming
  text, duplicate job cards.
- **Progress cards stopped showing internal job names.** A tool job announced
  itself as "tool:gif complete"; it now reads "Gif complete".
- **Merging a silent clip no longer throws away the other clips' audio.**
  Merge Clips normalizes every input before stitching, but it did not normalize
  the stream layout — a clip with no audio came out video-only, and the
  stitcher takes its layout from the first file. A silent intro card in front
  of a talking video therefore produced a merge with no sound at all, reported
  as a success. Every clip now reaches the stitch with an audio track, silent
  or not.
- **The app survives browser page-translation.** Chrome, Edge, Safari and the
  Baidu/QQ equivalents rewrap text into elements React is still tracking, and
  React's next update then crashed the whole interface to an error screen —
  most easily in Chat, where streaming text changes constantly. Since the page
  declares itself as English, this was offered to every non-English speaker on
  first load. It no longer crashes. (This stops the crash; it does not
  translate the app.)
- **Captions stop being burned under the platform's own UI.** Caption margins
  were pixel offsets tuned for a 1920-tall frame and nothing ever checked them
  against the interface TikTok/Reels/Shorts draw *on top of* the video. Three
  styles (Classic, Minimal, Karaoke) sat inside the band covered by the
  username, caption and music ticker — unreadable, and impossible to fix after
  export. A platform safe zone is now applied as a **floor**: styles that
  already clear the chrome are untouched, only the unsafe ones move, and the
  insets are fractions of the frame so they hold on a square, a 4:5 or a 4K
  render. The hook overlay gets the same treatment against the top bar.
- **Square videos got vertical caption geometry.** The tool runners' aspect
  probe was binary ("is it landscape?"), so a 1080x1080 file was treated as
  9:16 — wrong margins, wrong font size, and the vertical chrome inset a
  square feed post doesn't need.
- **Imported and translated subtitle cues no longer merge or drift.** Cues
  written back-to-back rendered as one long caption line showing text long
  before its own cue time, and overlapping cues (rolling auto-captions overlap
  by construction) pushed the timeline further out with every cue — a 200-cue
  import produced a caption track roughly twice the video's length, so the
  back half never rendered and everything before it desynced.
- **The caption style list is served from the render engine.** `/api/captions/
  styles` was a hand-maintained copy and had drifted: 7 of 10 styles, with four
  of them advertising a centre alignment the renderer never produces. Custom
  and AI-generated styles are also forced onto a bottom alignment, without
  which the safe-zone floor silently does nothing.
- **Cancelling a clip extraction actually cancels it.** Cancel only flipped the
  database row; the pipeline kept burning Whisper, the AI call and N parallel
  ffmpeg re-encodes, saved every clip, and overwrote "cancelled" with
  "success". It now stops at the next phase boundary, deletes the clips it had
  already produced, and stays cancelled.
- **Clip files orphaned by a crash are swept.** Clips are written straight into
  the generated-media directory under their final names, so a backend killed
  mid-run leaked them forever. A boot-time sweep reclaims `clip_*` files older
  than 24h that no library row references — including, deliberately, cached
  16:9 exports, which are themselves `clip_*` files referenced by a different
  column.
- **A manual cut with captions off no longer transcribes the whole source.**
  Hand-picked ranges need no transcript unless captions or silence-removal
  consume it, but Whisper ran anyway — on the first cut of a newly imported
  video that meant minutes of work to produce a seven-second trim.
- **"Remove silence & filler words" works for hand-picked ranges.** The option
  was gated out of manual mode on the theory that it would shift the user's
  chosen timing. It doesn't: silence is removed *inside* each already-cut clip,
  so the picked boundaries are untouched. The gate only made hand-picked clips
  the one place pacing couldn't be tightened.
- **Audio enhancement stopped degrading already-clean audio.** Single-pass
  loudness normalisation runs a different algorithm that rides the gain
  continuously — audible pumping, and it crushed loudness range a little more
  on every pass. Enhancement is now measure-then-apply with a single fixed
  gain, and audio already at target is returned untouched instead of being
  re-processed.
- **Background music you can actually hear.** The music bed sat 20dB down,
  which measured as a 0.1dB change to the finished mix — reported as "no
  background music", because there effectively wasn't any.
- **Silent substitutions now announce themselves.** Picking a music genre with
  no matching track quietly used any track at all, and a voice provider
  without a key quietly narrated in a different voice. Both now say so.
- **Aspect conversion stops re-encoding audio.** Audio is untouched by a
  geometry change, but every reframe/export paid a lossy generation for it,
  and chained tools stacked them.
- **Downloads use whatever JavaScript runtime the machine has.** yt-dlp was
  handed a hardcoded `node`, and passing that option *replaces* yt-dlp's own
  default — so a machine with deno but not Node ended up with no runtime at
  all, and a machine with neither got no warning. Without a runtime YouTube's
  n-signature challenges go unsolved and formats go missing or 403. Node and
  deno are now discovered, and a machine with neither gets one clear warning
  naming the consequence.
- **An HTTP 403 caused by cookies is now retried without them.** Supplying any
  cookie makes yt-dlp skip every player client that can't carry one — which are
  exactly the token-free ones — so a 403 was retried with the same losing
  configuration until the attempts ran out.
- **curl-cffi floor raised to 0.15.** 0.14 resets a libcurl handle from a
  done-callback while the calling thread is still reading it, which aborts the
  whole backend process — no traceback, just a dead app. TikTok forces
  impersonation, so it was reachable from every TikTok probe and download.
- **A translation that fails on every line is an error, not a silent
  passthrough.** Degrading a few captions to their source text is the point of
  the retry ladder; degrading all of them would burn a full render to hand back
  the original captions under a "translated" label.
- **Translating a long video no longer dies on one bad batch.** Translation
  sent every segment in a single AI call and enforced a strict 1:1 count by
  raising, so a long video overflowed the token budget and a model that merged
  two lines threw away the Whisper pass and everything already translated.
  Segments now go in batches of 20, a batch that comes back the wrong length is
  split and retried in halves down to single lines, and a line that fails even
  alone keeps its source text instead of sliding every later caption off its
  timestamp.
- **Burning captions onto a long video no longer times out at the last step.**
  The burn is a full re-encode on what was a flat 10-minute cap, so a 17-minute
  video failed after transcription (and, in the Translate tool, translation)
  had already run. The budget now scales with the source.
- **Metadata and Auto Chapters show their result inline again.** Both previews
  read a field the store never wrote, so both always fell through to "click
  Download instead" — the copy-to-clipboard preview, which is the whole point
  of a titles/tags/chapters tool, was unreachable code. They now read the job's
  own output, which also means the preview survives a page reload.
- **The Captions tool offers every caption style.** The renderer has ten; the
  endpoint validated three and the picker listed three, so neon, karaoke, glow,
  Bold Urban, Warm Glow, Monochrome and Minimal were unreachable from the tool
  built to apply them (and posting one came back 422). The accepted list is now
  derived from the engine, with a test pinning the API, both pickers and the
  Smart Video config list to it.
- **Reframe's description matches what it does.** The page advertised
  MediaPipe face-tracking with a center-crop fallback; the tool is a blur-fill
  fit and this build ships no face detection at all.
- **The Audio tools accept audio.** Enhance Audio and Silence Remover validated
  uploads against the video extensions only, so a podcast mp3 was rejected with
  a 400 *after* the upload — on the two tools whose whole job is the audio
  track. Both now take video or mp3/wav/m4a/aac/ogg/flac, and audio in means
  mp3 out.
- **Enhance Audio works on WebM, and never reports success over an empty file.**
  `-c:v copy` into an .mp4 is invalid for VP8/VP9 — and .webm is what most
  screen recorders export — so the job failed with a raw ffmpeg codec-tag
  error. It now retries with an H.264 re-encode, refuses to finish over a
  missing/0-byte output, and says plainly when a file has no audio track
  instead of surfacing a filtergraph error.
- **Silence removal is budgeted against the source length.** The ffmpeg pass
  re-encodes the whole timeline but carried a flat cap sized for 30-second
  clipper clips, so a long recording died on a raw `TimeoutExpired` with the
  entire command line in the message. The budget now scales with the source
  (measured, not "where the last word ends"), with a floor, a 30-minute
  ceiling, and a readable message if it is ever hit.
- **The Library no longer returns a short (or empty) page after self-healing.**
  The prune that removes rows whose file is gone ran inside the fetched page
  and returned what was left, so a library with enough stale rows could answer
  "no videos" next to a total of several hundred. It now re-queries after
  committing the deletes, so you get a full page.
- **Clip Studio asks the backend for clips.** It pulled an unfiltered page of
  100 videos and filtered in the browser, so enough recent non-clip rows
  rendered "No clips yet" over a library full of clips. `GET /api/videos` takes
  a `source_type` filter now.
- **A video's niche is a keyword again, not a paragraph.** The generator stored
  the analyzer's `topic_angle` — prose by design — in the niche column. It now
  resolves a real short niche (the source's own, else the scout query that
  found it) or stores nothing.
- **Transcription shows live progress instead of looking hung.** Whisper's
  segment generator was consumed in one gulp, so a 40-minute audio sat at a
  frozen 7% / 15% for the entire 15–30 minute transcription — indistinguishable
  from a hang, and reported as one. Analyze and Clip Studio now show a real
  percentage as the decode advances (the analyzer writes it to the DB too, so
  the polled jobs list moves as well as the live socket). Two *long*
  transcriptions also now queue explicitly instead of thrashing the machine
  invisibly inside ctranslate2; short ones still run concurrently, so a caption
  pass never waits behind a batch job.
- **Caption style "None" really means no captions.** Clip Studio's "none" chip
  (which even greys out AutoEmoji when you pick it) was never treated as a
  sentinel backend-side: the ASS builder falls back to the "viral" preset for
  any style name it doesn't recognise, so every "no captions" extraction came
  back with fully burned-in yellow word-by-word subtitles. "None" now skips the
  burn entirely — including the hook overlay, which rides the same file.
- **Extracted clips are labelled with their real shape and length.** Every clip
  row was written as 9:16 with the requested window's duration. Extraction only
  reframes a *landscape* source, so square and 4:5 sources kept their own shape
  and were mislabelled — the Library sizes each tile from that column — and
  "Remove silence" cuts content out, so the stored duration overstated the file.
  Both are now probed from the finished clip, which also stops the thumbnail
  timestamp landing past the end of a heavily-trimmed clip.
- **AI metadata can no longer overwrite a clip's own fields.** The model's
  metadata was merged into the clip record last and unfiltered, so any key it
  invented won — including `video_path`, the file we persist and serve. Only
  the four requested fields are kept now.
- **Exporting a vertical video to 16:9 no longer shrinks the picture.** The
  export hardcoded the blur-fill look, which FITS the whole source inside the
  target frame. Going the other way (16:9 → 9:16) that is exactly right and it
  is how every short is built — but widening a 9:16 short left the content as a
  narrow strip with blur either side, and because ViralMint shorts are
  themselves blur-fill composites the export nested a second box and the
  picture landed at about a third of the frame. Exports now default to `auto`:
  crop when widening, blur-fill when narrowing. An explicit `method` still wins.
- **Reframe to Vertical works on square and 4:5 sources.** The "already
  vertical" short-circuit tested `width <= height`, so a 1080x1080 or 1080x1350
  clip came back byte-identical with an "already vertical" notice instead of
  being cropped to 9:16. Only 9:16-or-narrower has nothing to crop.
- **Removed a dead duplicate export route.** A second
  `POST /api/videos/{id}/export` handler had been shadowed by the first since
  the day it was added; its one useful behaviour (caching the 16:9 render for
  later streaming) now lives in the live handler.
- **TikTok channels show their full video list.** The My Channels grid capped
  TikTok at 20 videos while the YouTube side fetched 200. Both are 200 now; the
  TikTok scrape is a single request either way.
- **Clip Studio: two videos no longer play at once.** Opening the source-video
  preview while a clip was playing left both running and both audible.
- **Best-posting-time recommendation no longer crashes.** As soon as you had
  upload history the endpoint raised a `TypeError` — it rounded a list of view
  counts instead of the per-day average it had already computed — so the whole
  feature was unreachable on its success path.
- **Subtitles tool no longer loses your job when you navigate away.** The
  Subtitles page tagged its job `tool:subtitles` while the backend creates
  `tool:subtitle_export`, so leaving the page mid-run and coming back showed the
  empty upload state as if the file had never been submitted. The endpoint →
  job-type mapping now lives in one place, checked against the real backend
  handlers by a test.
- **Whisper quality setting is actually used.** Picking "accurate" or "best"
  loaded that model and then immediately threw it away and re-loaded "small",
  so analysis silently ran at the default quality while paying for two model
  loads. Transcription quality is now passed explicitly at every call site.
- **No more duplicate Whisper model downloads.** The "is this model already
  downloaded?" check hardcoded `~/.cache/huggingface` and ignored `HF_HOME`, so
  on any setup that relocates the cache an already-present model looked missing
  and a second full copy (up to ~3 GB) was fetched.
- **Heavy Whisper models no longer pin memory forever.** `medium`/`large-v3`
  stayed resident for the life of the process — ~3 GB of RSS held by an app that
  otherwise sits idle in the tray. They're now evicted after 10 minutes unused.
- **Restart no longer fails a job that is still running.** A tray Restart (or
  any port takeover) booted a second backend whose startup sweep marked every
  in-flight job "Server restarted — job did not complete" — but uvicorn frees
  its port at the *start* of graceful shutdown, so the old process was often
  still draining and went on to finish the job. You saw a failure toast for a
  video that actually landed in the Library, and a retry redid all the work.
  Jobs now carry a heartbeat (`jobs.updated_at`, refreshed by every progress
  tick); the boot sweep fails only jobs whose heartbeat has gone stale, and a
  background watcher re-checks the survivors until they finish or go stale. The
  launcher also waits for the old process to actually exit, not just to release
  the port. A late progress tick can no longer resurrect a finished job.
- **Job progress survives a page refresh.** Progress steps were broadcast over
  the WebSocket but never written to the jobs table, so reloading mid-job (or a
  WebSocket reconnect) showed the stale "Loading source data..." baseline for
  the rest of the run.
- **Scout hardening ported from the hosted variant.** Fixes a timezone crash
  in virality scoring (tz-aware feed dates), makes outlier enrichment
  non-fatal with an `author_url` None-guard, shows every scouted result on a
  repeat scout (not just net-new rows), adds a 60s ceiling + extract fallback
  to the yt-dlp search path, retries empty news-RSS passes, caps/de-dupes the
  platform list, and surfaces the cross-post fallback as a constraint warning.
- **Static-asset caching + upgrade refresh.** Content-hashed assets are served
  `immutable` (no revalidation) while `index.html` is `no-cache`, so normal
  loads are fast and an app upgrade refreshes on first reload.
- **Schema-drift warning.** Startup now logs a loud warning if a model column
  is missing from the live DB, catching a forgotten migration early.
- **`VIRALMINT_DATA_DIR`.** The DB, storage, and `.env` location now honor
  `VIRALMINT_DATA_DIR` (falling back to the working directory when unset).

### Added
- **Clip Studio — structured scoring + control knobs ported from the hosted variant.**
  Clips now get a hook score + hook type and a flow/value/trend/shareability
  score breakdown (new `clip_hook_score` / `clip_hook_type` /
  `clip_score_breakdown_json` columns, auto-migrated). The extract dialog gains
  a free-form "describe the clips you want" query, target-platform and genre
  bias, an emoji-style control, a remove-silence toggle, and a manual mode for
  extracting explicit time ranges. Extraction options are consolidated into a
  single `ExtractOptions` object; each clip gets a descriptive title and an
  optional on-screen hook overlay.
- **Chat — rich cards now persist across reloads.** The backend became the
  single writer of rich cards (scout results, channel analysis, …) and
  job-complete rows at WS-emit time, so they survive a page reload and are
  saved even when a job finishes with no tab open (previously they were
  in-memory only and lost).
- **Chat — quick-reply chips** and a composer-lock fix: when the assistant asks
  a follow-up question (e.g. "which platform?"), the input no longer stays
  locked, and suggested answers render as clickable chips.
- **Clip Studio — selection-quality improvements ported from the hosted variant.**
  Sentence-snap (clips no longer cut mid-word), silent-gap backfill, topic
  dedup (drops re-told stories), a short-video fast-path (sources under 20s
  emit the whole clip; the blanket <30s reject is gone), and batched clip
  metadata (one AI call for N clips instead of N). No-speech sources now yield
  duration-based clips instead of erroring.
- **Captions — CJK homophone correction.** When the narration script is
  CJK-dominant, the burned captions now use the true script text (keeping
  Whisper's timings) instead of ASR homophone substitutions. Fail-open for
  non-CJK content.

### Fixed
- **Clip Studio — extraction hardening ported from the hosted variant (7 bugfixes).**
  Fixes a `time_offset` double-count that silently dropped almost every clip
  past the first chunk on long videos; a clip-count estimator that assumed 40s
  clips (collapsing "3×15s from a 63s video" to 1); Whisper failures that
  silently downgraded to random duration-based clips instead of failing loudly;
  single-bound (min-only / max-only) duration overrides being ignored; the
  retry cascade widening past user-pinned bounds; and two caption/exception
  leaks into the output path. Adds `backend/core/concurrency.py` to cap
  parallel ffmpeg work.
- **Analyzer — chunked AI transcript correction.** The old single-call
  correction on `raw_text[:6000]` silently discarded everything past 6000 chars
  on long videos; now sentence-aligned chunking corrects the whole transcript
  with a per-chunk sanity guard (never loses content). Plus a `has_audio_stream`
  ffprobe preflight so a video-only/silent file raises a clear error instead of
  faster-whisper's opaque "tuple index out of range".
- **Captions — placement, flashing, and non-Latin fixes.** `alignment=5`
  (frame-center, ignores margins) → `alignment=2` (bottom-anchored) with
  per-aspect margins; phrase-aware line grouping with continuous-hold events so
  captions no longer blank out during Whisper's inter-word gaps; script-aware
  font fallback so CJK/Arabic/Thai captions stop burning as tofu boxes; libass
  preflight; concurrency-safe temp file; new `brainrot`/`urban`/`warm`/`mono`
  styles.
- **Music mix — voiceover level.** `amix` defaulted `normalize=1`, halving the
  voiceover to −6 dB; add `normalize=0` + an `alimiter` peak guard so the voice
  stays full-level with music as a true −20 dB bed.
- **Messaging — concurrent channel start.** `start_all()` now launches every
  channel in parallel with per-channel failure isolation, so the slowest
  channel no longer gates the rest.
- **Download hardening — pinned yt-dlp floor + TLS impersonation.**
  `requirements.txt` now pins `yt-dlp>=2026.7.4`: an unbounded `yt-dlp` on an
  old Python (macOS's system `python3` is 3.9) silently resolves to an ancient
  2025.10 release that fails on modern YouTube — the floor turns that into a
  loud install error instead of a broken downloader. Added
  `curl-cffi>=0.10,<0.15` and wired Chrome TLS impersonation into every
  yt-dlp call (`ytdlp_service`), so TLS-fingerprinting bot defenses
  (Cloudflare/Akamai) can't block downloads by handshake; degrades cleanly to
  urllib's fingerprint if curl-cffi is missing or incompatible.
- **Download reliability port from the hosted variant** (`ytdlp_service`):
  original-audio `format_sort` with `lang` leading (multi-language YouTube
  videos no longer download a dubbed audio track), exponential
  `retry_sleep_functions` per retry-pool, a 100 KB/s `throttledratelimit`
  guard that re-extracts stale signed URLs, and per-extractor args —
  PO-token-aware YouTube `player_client` ordering (token-free clients lead),
  `youtubetab` authcheck skip for public channel extraction, TikTok
  genuine-device-id flow, Twitter syndication API, Instagram/Reddit retry
  bumps. The pip self-update is now version-bounded (`yt-dlp>=2026.7.4`) so
  an outdated Python can't silently downgrade the downloader.

### Added
- **Tools page** — 18 single-purpose utilities (captions, reframe, audio-enhance, watermark, remove-silence, merge-clips, GIF, speed, trim, subtitles, auto-zoom, transform, music-visualizer, voice-over via Edge TTS, plus AI helpers: translate, metadata, hook-analysis, auto-chapters). The 13 ffmpeg/Whisper tools run fully locally with no API key; the AI helpers and the ✨ Enhance-prompt button use the user's own key (BYOK). Each tool has an inline result preview. New `/api/tools/*` router + `backend/core/tool_runners.py`. (AI media generators — image/music/video — are intentionally not in the OSS build.)
- **Proactive assistant** — the planner now reads live pipeline state (downloaded-not-clipped, generated-not-uploaded, scouted-not-downloaded) and surfaces the single highest-value next step. Backed by behavior-event instrumentation so the personalization engine learns from every completed job.

### Fixed
- Library self-heals — generated-video rows whose rendered file has been deleted are now pruned on list, so dead/broken tiles no longer linger.

### Security
- Bump `aiohttp` to `>=3.14.0,<4` — closes CVE-2026-34993 and CVE-2026-47265 (pip-audit). The frontend's `vite`/`esbuild` dev-server advisories are intentionally left for a future `vite` major bump: they affect only `npm run dev`, not the bundled app users ship, and the fix is a breaking change.
- Bump `cryptography` to `>=46.0.6,<47` — closes PYSEC-2026-35, GHSA-h4gh-qq45-vh27, CVE-2024-12797, CVE-2026-26007 (4 CVEs in the 43.x line).
- Bump `Pillow` to `>=12.2.0,<13.0` — closes CVE-2026-25990, 40192, 42308, 42309, 42310, 42311 (6 OOB / hang / memory-corruption issues affecting the thumbnail and ffmpeg image-processing paths). The `Image.ANTIALIAS` / `BICUBIC` monkeypatch in `backend/main.py` continues to work against Pillow 12.x.

### Changed
- Bump `openai` floor from `1.55` to `1.109.1` (still `<2.0`).
- Bump `playwright` floor from `1.58` to `1.59`.
- Bump 12 grouped Python minor/patch deps (dependabot `python-minor-patch` group).
- Bump `@mui/icons-material` 7.3.9→7.3.11, `axios`, `lucide-react` (dependabot `js-minor-patch` group).
- Bump CI actions — `actions/checkout` v4→v6 plus `setup-python`, `setup-node`, `codeql-action` (dependabot `ci-actions` group).

### Docs
- README — added an above-the-fold "Two ways to use ViralMint" callout clarifying the OSS variant (BYOK, Uploader agent, AGPL-3.0) vs the hosted SaaS at viralmint.net (prepaid credits, no auto-upload, closed-source). Helps new visitors pick the right variant without scrolling.

## [1.1.0] — 2026-05-07

### Added
- **OpenRouter as a third BYOK provider** — alongside Anthropic and OpenAI direct, a single OpenRouter API key now opens access to 300+ models (Claude, GPT, Gemini, Mixtral, Llama, etc.) through one credential. Configurable via `.env` or per-user in Settings → API Keys. See `backend/core/ai_provider.py`.

### Changed
- **Dependabot config** — minor/patch dependency updates are now batched into three groups (`python-minor-patch`, `js-minor-patch`, `ci-actions`) instead of arriving one PR at a time. Major framework versions (FastAPI / React / Pillow majors etc.) stay outside the groups so they always get explicit review.

## [1.0.0] — 2026-05-07

Initial open-source release.

### Added
- **Scout** — multi-platform trend discovery across YouTube, TikTok, Douyin, and Google Trends, with virality scoring and 3×–20× channel-baseline outlier detection.
- **Analyze** — local Whisper transcription plus AI insight extraction (hook, structure, tone, retention risks) per downloaded video.
- **Generate** — full pipeline: AI script → TTS voice → Pexels stock footage → word-by-word ASS captions → background music → finished mp4.
- **Clip Studio** — extract publishable 30–60s shorts from a long-form source; AI picks the best moments and burns captions.
- **Publish** — direct upload to YouTube (OAuth) and TikTok (OAuth or session cookie) with platform-optimized titles, descriptions, tags, and thumbnails.
- **Chat** — streaming WebSocket chat with the planner agent; action blocks dispatch background jobs (scout / download / analyze / generate / upload).
- **Messaging** — two-way chat over Telegram, WhatsApp, Discord, and Slack — same agent, different transport.
- **BYOK** — Anthropic / OpenAI / YouTube / Pexels / TikHub keys settable per-user in the UI or via `.env`. Per-user keys are AES-256 encrypted at rest.
- **Edge TTS** — 400+ free voices in 70+ languages; the default voiceover provider.
- **Universal downloader** — yt-dlp under the hood (1000+ sites supported).
- 92-test pytest suite covering crypto, scout scoring, captions, exception handling, HTTP utilities, and the async task runner.
- AGPL-3.0 license, SPDX headers on every Python source file.

### Security
- API binds to `127.0.0.1` (loopback) by default. Users who want LAN access can set `HOST=0.0.0.0` in `.env` knowingly.
- All third-party credentials encrypted with Fernet (AES-256) before being written to SQLite.
- No telemetry. No analytics. No cloud backend in the middle — keys go directly from your machine to the provider.

[Unreleased]: https://github.com/openclaw-easy/ViralMint/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/openclaw-easy/ViralMint/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/openclaw-easy/ViralMint/releases/tag/v1.0.0

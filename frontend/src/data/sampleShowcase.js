// Curated "start from a sample" concepts for the vivid creation-page showcase
// (PivotPlan §9/§11 — "show value, don't explain it"). Each sample is a palette
// + a short HOOK that animates in the thumbnail, a `bgQuery` (Pexels search term
// for the real footage that plays behind the caption), and a ready-to-generate
// SCRIPT + matching settings. Clicking one ("Make one like this") drops the
// script + settings into the editor so the user can hit Generate immediately —
// no blank-page paralysis.
//
// Rendering is layered (see SampleShowcase.jsx): an instant CSS gradient, a real
// Pexels clip whose url is resolved SERVER-SIDE via /api/showcase/clip?q=<bgQuery>
// (the Pexels key stays in the cloud — desktop never touches it) and fades in
// when it plays, plus a canvas caption on top. Procedural gradient stays if the
// cloud returns no url (signed out / offline). We ship NO video assets. This
// array is the single swap point — edit/extend it to refresh the showcase.
//
// `apply` fields map 1:1 to SmartVideo state: visualStyle ∈ STYLES ids
// (cinematic/vlog/card/educational/documentary), captionStyle ∈ viral/bold/classic,
// musicGenre ∈ upbeat/lofi/cinematic, aspectRatio "9:16".

export const SMART_VIDEO_SAMPLES = [
  {
    id: "rice-hack",
    label: "Rice hack",
    niche: "Cooking",
    bgQuery: "cooking food kitchen",
    hook: "You've been cooking rice WRONG",
    cA: "#C58A4A",
    cB: "#5C3E20",
    script:
      "Stop boiling your rice like that. First, rinse it three times until the water runs clear — that's the secret to fluffy grains. Use one and a half cups of water per cup of rice. Bring it to a boil, then cover and drop to low for twelve minutes. Don't lift the lid. Let it rest five minutes off the heat, then fluff with a fork. Perfect every single time.",
    apply: { visualStyle: "vlog", captionStyle: "viral", musicGenre: "upbeat", aspectRatio: "9:16" },
  },
  {
    id: "back-fix",
    label: "5-min back fix",
    niche: "Fitness",
    bgQuery: "home workout fitness",
    hook: "Fix your back in 5 minutes",
    cA: "#2B4566",
    cB: "#B7D2E8",
    script:
      "If your lower back aches by noon, do this every morning. Lie on your back and pull one knee to your chest, hold for thirty seconds, then switch. Move into cat-cow for one minute to wake up your spine. Finish with a thirty-second glute bridge. Five minutes total. Do it for one week and feel the difference. Your future self will thank you.",
    apply: { visualStyle: "educational", captionStyle: "bold", musicGenre: "lofi", aspectRatio: "9:16" },
  },
  {
    id: "ai-email",
    label: "AI email trick",
    niche: "Tech / AI",
    bgQuery: "technology laptop typing",
    hook: "This free AI writes your emails",
    cA: "#1E1B2E",
    cB: "#0A0810",
    script:
      "You're wasting an hour a day on email. Here's a free fix. Open any AI chat, paste the message you got, and type: reply politely and keep it short. In two seconds you get a draft. Tweak one line and send. I went from forty minutes in my inbox to ten. The tool is free — the time you save is not.",
    apply: { visualStyle: "card", captionStyle: "viral", musicGenre: "upbeat", aspectRatio: "9:16" },
  },
  {
    id: "rome-fell",
    label: "Why Rome fell",
    niche: "History",
    bgQuery: "ancient rome ruins",
    hook: "Why Rome REALLY fell",
    cA: "#4A5568",
    cB: "#1A202C",
    script:
      "Rome didn't fall in a day, and it wasn't just the barbarians. The empire debased its own currency until money was worthless. It overstretched its borders past what its armies could defend. And it leaned on forced labor that hollowed out the middle class. Sound familiar? Empires rarely die from invasion. They rot from the inside. Follow for more history that hits different.",
    apply: { visualStyle: "documentary", captionStyle: "bold", musicGenre: "cinematic", aspectRatio: "9:16" },
  },
  {
    id: "save-1000",
    label: "Saved $1,000",
    niche: "Finance",
    bgQuery: "money cash saving",
    hook: "I saved $1,000 in 30 days",
    cA: "#1E1B2E",
    cB: "#0A0810",
    script:
      "I saved a thousand dollars in one month with zero extra income. First I canceled three subscriptions I'd forgotten about — sixty bucks gone. I cooked every meal for two weeks and saved four hundred. Then I set one auto-transfer of twenty dollars a day into a separate account. Out of sight, out of mind. Thirty days later: a thousand dollars. Small moves, real money.",
    apply: { visualStyle: "card", captionStyle: "viral", musicGenre: "lofi", aspectRatio: "9:16" },
  },
  {
    id: "cheap-trip",
    label: "Cheapest trip",
    niche: "Travel",
    bgQuery: "tropical beach travel",
    hook: "A week abroad under $500",
    cA: "#1B5E5F",
    cB: "#C2552E",
    script:
      "Want a week abroad for under five hundred dollars? Go to Vietnam. A bowl of pho costs two dollars. A beachfront room runs fifteen a night. A motorbike to explore is five. The coastline rivals anywhere in the world and the coffee is unreal. Fly in, slow down, and watch your money last twice as long. Save this for your next trip.",
    apply: { visualStyle: "cinematic", captionStyle: "classic", musicGenre: "upbeat", aspectRatio: "9:16" },
  },
  {
    id: "delete-apps",
    label: "Delete 3 apps",
    niche: "Productivity",
    bgQuery: "smartphone phone desk",
    hook: "Delete these 3 apps NOW",
    cA: "#1E1B2E",
    cB: "#0A0810",
    script:
      "Delete these three apps right now and get an hour back every day. First, any game with a daily login streak — it's engineered to hook you. Second, the shopping app you open out of boredom. Third, the news app that just makes you anxious. Replace them with one book on your home screen. Watch what happens in a week.",
    apply: { visualStyle: "card", captionStyle: "viral", musicGenre: "upbeat", aspectRatio: "9:16" },
  },
  {
    id: "black-hole",
    label: "Inside a black hole",
    niche: "Space",
    bgQuery: "galaxy space stars",
    hook: "What's INSIDE a black hole?",
    cA: "#4A5568",
    cB: "#1A202C",
    script:
      "What's actually inside a black hole? Nobody knows, and that's the terrifying part. Past the event horizon, gravity is so strong that not even light escapes. Time slows to a crawl. At the center sits a singularity, where the physics we trust completely breaks down. A black hole isn't a hole — it's a one-way door out of the universe.",
    apply: { visualStyle: "documentary", captionStyle: "bold", musicGenre: "cinematic", aspectRatio: "9:16" },
  },
  {
    id: "five-second-rule",
    label: "Beat procrastination",
    niche: "Psychology",
    bgQuery: "person walking morning sunrise",
    hook: "The 5-second rule that works",
    cA: "#2B4566",
    cB: "#B7D2E8",
    script:
      "Here's the five-second rule that kills procrastination. The moment you feel the urge to act on a goal, count backwards — five, four, three, two, one — then move. Those five seconds interrupt the part of your brain that talks you out of it. It sounds too simple to work. Try it once tomorrow morning and you'll see why it does.",
    apply: { visualStyle: "educational", captionStyle: "bold", musicGenre: "lofi", aspectRatio: "9:16" },
  },
  {
    id: "cheap-skincare",
    label: "$8 skincare",
    niche: "Skincare",
    bgQuery: "skincare beauty routine",
    hook: "The $8 product derms love",
    cA: "#C58A4A",
    cB: "#5C3E20",
    script:
      "Dermatologists keep recommending the same eight-dollar product, and most people walk right past it. It's plain moisturizing cream with three ceramides that rebuild your skin barrier. No fancy claims. Use it morning and night, right after washing. Within two weeks the dry patches fade and your skin stops feeling tight. Good skincare doesn't have to cost a fortune.",
    apply: { visualStyle: "vlog", captionStyle: "viral", musicGenre: "lofi", aspectRatio: "9:16" },
  },
]

export default SMART_VIDEO_SAMPLES

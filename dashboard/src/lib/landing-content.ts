export const siteMeta = {
  title: 'tomo, the thoughtful companion',
  description: 'meet tomo, a stalker, an assistant, a friend.',
  icon: '/images/favicon.png',
};

export const navLinks = [
  { href: '#how-it-works', label: 'how it works' },
  { href: '#about-tomo', label: 'about tomo' },
];

export const hero = {
  eyebrow: 'introducing tomo 1.0',
  headline: 'meet tomo,',
  highlights: ['a stalker,', 'an assistant,', 'a friend'],
  cta: 'text tomo',
};

export const principles = [
  {
    title: 'privacy by design',
    body: 'your conversations are yours alone. tomo only keeps what you choose to share.',
    tone: 'dark',
  },
  {
    title: 'seamless integration',
    body: 'connect calendar, notes, and your local environment so tomo can act naturally.',
    tone: 'accent',
  },
] as const;

export const features = [
  {
    title: 'authentic connection',
    body: 'moving beyond commands. tomo learns your preferences, mood, and subtle cues to feel genuinely supportive.',
    accentEdge: 'left',
  },
  {
    title: 'intuitive support',
    body: 'anticipating needs before they are voiced. from schedules to pauses, tomo aligns with your natural pace.',
    accentEdge: 'right',
    reverse: true,
  },
  {
    title: 'grounded intelligence',
    body: 'no hallucinations, just clarity. tomo uses verified data and local context to stay accurate and useful.',
    accentEdge: 'top',
  },
] as const;

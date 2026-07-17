export const siteMeta = {
  title: 'tomo | your context, carried forward',
  description: 'A private personal assistant for keeping track of the details you choose to share.',
};

export const hero = {
  cta: 'text tomo',
  image: 'https://images.unsplash.com/photo-1464278533981-50106e6176b1?auto=format&fit=crop&w=2400&q=90',
};

export const principles = [
  {
    title: 'Your context stays yours.',
    body: 'Tomo works only with the context you choose to share. privacy by design means your conversations are not a product.',
    tone: 'ink',
  },
  {
    title: 'Useful before it is loud.',
    body: 'A considered reminder or an opened loop is enough. The point is less management and more room to keep moving.',
    tone: 'paper',
  },
] as const;

export const signalTrace = [
  {
    title: 'Say what is on your mind.',
    body: 'Share the details that matter, in a conversation that can continue when you return.',
  },
  {
    title: 'Bring the right things together.',
    body: 'Tomo can use the connections you choose to make the next step clearer.',
  },
  {
    title: 'Pick up where you left off.',
    body: 'A useful thread does not disappear just because the day got busy.',
  },
] as const;

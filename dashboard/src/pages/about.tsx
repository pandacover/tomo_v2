import { Link } from 'waku';

export default async function AboutPage() {
  const data = await getData();

  return (
    <div>
      <title>{data.title}</title>
      <h1 className="font-heading text-4xl font-black tracking-tight">{data.headline}</h1>
      <p className="mt-3 max-w-prose text-tomo-ink/70">{data.body}</p>
      <Link to="/" className="mt-4 inline-block underline">
        return home
      </Link>
    </div>
  );
}

const getData = async () => {
  const data = {
    title: 'about tomo',
    headline: 'about tomo',
    body: 'a tiny page kept for framework routing checks.',
  };

  return data;
};

export const getConfig = async () => {
  return {
    render: 'static',
  } as const;
};

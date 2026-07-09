import type { principles } from '../lib/landing-content';

type Principle = (typeof principles)[number];

type PrincipleCardProps = Principle & {
  className?: string;
};

export const PrincipleCard = ({ title, body, tone, className = '' }: PrincipleCardProps) => {
  const classes =
    tone === 'dark'
      ? 'hover-card border-tomo-ink bg-tomo-ink text-white shadow-[12px_12px_0_var(--color-tomo-accent)]'
      : 'hover-card-black border-tomo-ink bg-tomo-accent text-tomo-ink shadow-[12px_12px_0_var(--color-tomo-ink)]';

  return (
    <article className={`animate-reveal border-2 p-8 md:p-12 ${classes} ${className}`}>
      <h2 className="mb-6 font-heading text-4xl font-black text-current">
        {title}
      </h2>
      <p className="max-w-md text-lg leading-relaxed opacity-80">{body}</p>
    </article>
  );
};

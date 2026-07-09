import type { features } from '../lib/landing-content';

type Feature = (typeof features)[number];

type FeatureRowProps = Feature & {
  className?: string;
};

const edgeClass = {
  left: 'border-l-8 border-l-tomo-accent',
  right: 'border-r-8 border-r-tomo-ink',
  top: 'border-t-8 border-t-tomo-accent',
} as const;

export const FeatureRow = ({
  title,
  body,
  accentEdge,
  reverse = false,
  className = '',
}: FeatureRowProps) => {
  return (
    <section className={`animate-reveal grid items-center gap-8 md:grid-cols-2 ${className}`}>
      <div className={reverse ? 'md:order-2 md:text-right' : ''}>
        <h2 className="font-heading text-6xl font-black leading-none tracking-tight text-tomo-ink md:text-8xl">
          {title}
        </h2>
      </div>
      <div className={reverse ? 'md:order-1' : ''}>
        <div className={`border border-tomo-ink/10 bg-white p-8 md:p-10 ${edgeClass[accentEdge]}`}>
          <p className="text-xl leading-relaxed text-tomo-ink/80">{body}</p>
        </div>
      </div>
    </section>
  );
};

import { hero } from '../lib/landing-content';

export const LandingHero = () => {
  return (
    <section className="kinetic-layer mx-auto max-w-tomo-container px-tomo-gutter py-20 md:py-24">
      <div className="scatter-1 mx-auto flex max-w-5xl flex-col items-center space-y-10 text-center">
        <div className="animate-reveal inline-flex items-center gap-2 rounded-full border border-tomo-ink/10 bg-tomo-accent px-6 py-2 text-tomo-ink shadow-sm [animation-delay:100ms]">
          <span aria-hidden="true" className="text-lg leading-none">
            ✦
          </span>
          <span className="font-heading text-sm font-black uppercase tracking-[0.2em]">
            {hero.eyebrow}
          </span>
        </div>
        <h1 className="text-balance font-heading text-[4.5rem] font-black leading-[0.9] tracking-tighter text-tomo-ink md:text-[7.5rem]">
          <span className="animate-reveal inline-block [animation-delay:200ms]">
            {hero.headline}
          </span>
          <br />
          {hero.highlights.map((line, index) => (
            <span
              key={line}
              className="animate-reveal mt-2 inline-block bg-tomo-ink px-4 py-1 text-tomo-accent"
              style={{ animationDelay: `${300 + index * 100}ms` }}
            >
              {line}
            </span>
          ))}
        </h1>
        <div className="scatter-2 animate-reveal pt-4 [animation-delay:600ms]">
          <a
            className="inline-flex bg-tomo-ink px-12 py-5 font-heading text-xl font-black uppercase tracking-widest text-white shadow-[8px_8px_0_var(--color-tomo-accent)] transition-colors hover:bg-tomo-accent hover:text-tomo-ink active:scale-95"
            href="/login?next=/api/onboarding/telegram"
          >
            {hero.cta}
          </a>
        </div>
      </div>
    </section>
  );
};

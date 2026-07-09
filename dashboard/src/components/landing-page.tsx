import { features, principles } from '../lib/landing-content';
import { FeatureRow } from './feature-row';
import { FloatingGlyphs } from './floating-glyphs';
import { LandingHero } from './landing-hero';
import { PrincipleCard } from './principle-card';

const scatterClasses = [
  'scatter-1 [animation-delay:900ms]',
  'scatter-3 [animation-delay:1000ms]',
  'scatter-4 [animation-delay:1100ms]',
];

export const LandingPage = () => {
  return (
    <>
      <FloatingGlyphs />
      <LandingHero />
      <section id="how-it-works" className="kinetic-layer mx-auto max-w-tomo-container px-tomo-gutter py-20">
        <div className="grid gap-12 md:grid-cols-2">
          {principles.map((principle, index) => (
            <PrincipleCard
              key={principle.title}
              {...principle}
              className={`${index === 0 ? 'scatter-3 [animation-delay:700ms]' : 'scatter-4 md:mt-24 [animation-delay:800ms]'}`}
            />
          ))}
        </div>
      </section>
      <section id="about-tomo" className="kinetic-layer px-tomo-gutter py-24 md:py-32">
        <div className="mx-auto max-w-tomo-container space-y-24 md:space-y-32">
          {features.map((feature, index) => (
            <FeatureRow
              key={feature.title}
              {...feature}
              className={scatterClasses[index]}
            />
          ))}
        </div>
      </section>
    </>
  );
};

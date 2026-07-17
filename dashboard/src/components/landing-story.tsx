'use client';

import Link from 'next/link';
import { motion, useReducedMotion, useScroll, useTransform } from 'motion/react';
import { useRef } from 'react';
import { principles, signalTrace } from '@/lib/landing-content';

export function LandingStory() {
  const storyRef = useRef<HTMLElement>(null);
  const reduceMotion = useReducedMotion();
  const { scrollYProgress } = useScroll({
    target: storyRef,
    offset: ['start end', 'end start'],
  });
  const foregroundY = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [-18, 28]);
  const reveal = reduceMotion ? false : { opacity: 0, y: 20 };

  return (
    <section ref={storyRef} className="landing-story">
      <motion.div className="landing-foreground" style={{ y: foregroundY }}>
        <section id="trace" className="landing-trace" aria-labelledby="trace-heading">
          <div className="landing-story-inner landing-trace-grid">
            <motion.div
              className="landing-trace-intro"
              initial={reveal}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, amount: 0.35 }}
              transition={{ duration: 0.65, ease: [0.16, 1, 0.3, 1] }}
            >
              <p className="landing-section-label">the trace</p>
              <h2 id="trace-heading">The small things can stay in reach.</h2>
              <p>A calm place to leave a thought, carry it forward, and return when it becomes useful.</p>
            </motion.div>

            <ol className="landing-trace-moments">
              {signalTrace.map((signal, index) => (
                <motion.li
                  key={signal.title}
                  initial={reveal}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.5 }}
                  transition={{ duration: 0.55, delay: reduceMotion ? 0 : index * 0.08, ease: [0.16, 1, 0.3, 1] }}
                >
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <h3>{signal.title}</h3>
                    <p>{signal.body}</p>
                  </div>
                </motion.li>
              ))}
            </ol>
          </div>
        </section>

        <section id="principles" className="landing-principles" aria-labelledby="principles-heading">
          <div className="landing-story-inner">
            <motion.h2
              id="principles-heading"
              initial={reveal}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, amount: 0.4 }}
              transition={{ duration: 0.65, ease: [0.16, 1, 0.3, 1] }}
            >
              Less to <em>manage.</em><br />
              More room to live.
            </motion.h2>
            <div className="landing-principle-notes">
              {principles.map((principle, index) => (
                <motion.article
                  key={principle.title}
                  initial={reveal}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.55 }}
                  transition={{ duration: 0.55, delay: reduceMotion ? 0 : index * 0.1, ease: [0.16, 1, 0.3, 1] }}
                >
                  <p className="landing-section-label">{index === 0 ? 'kept private' : 'kept useful'}</p>
                  <h3>{principle.title}</h3>
                  <p>{principle.body}</p>
                </motion.article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-closing" aria-label="start a conversation">
          <div className="landing-story-inner">
            <p>Start with a conversation. The rest can take shape from there.</p>
            <Link className="landing-closing-link" href="/login?next=/api/onboarding/telegram">text tomo</Link>
          </div>
        </section>
      </motion.div>
    </section>
  );
}

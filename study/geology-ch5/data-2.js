(function () {
  var d = window.STUDY_DATA;
  if (!d || !d.questions) throw new Error('data-2.js: load data.js first');
  d.questions.push(
    {
      n: 3,
      type: 'mc',
      prompt: 'The Mercalli Scale is a scale from ________.',
      choices: [
        '1 to 12 that rates the energy required for faulting to occur',
        '1 to 10 that rates the energy released by an earthquake',
        'I to XII that rates the structural damage due to an earthquake',
        'I to X that rates the total energy released during the main quake and all aftershocks'
      ],
      answer: 2,
      explanation: 'The Modified Mercalli scale measures intensity, meaning observed effects and damage, in Roman numerals from I to XII.',
      concepts: ['intensity'],
      verify: false
    },
    {
      n: 4,
      type: 'mc',
      prompt: 'When an earthquake occurs, energy radiates in all directions from its source. The source is also referred to as the ________.',
      choices: [
        'inertial point',
        'epicenter',
        'focus',
        'seismic zone'
      ],
      answer: 2,
      explanation: 'The focus, also called the hypocenter, is the point underground where rupture starts.',
      concepts: ['focus'],
      verify: false
    },
    {
      n: 5,
      type: 'mc',
      prompt: 'Megathrusts between subducting oceanic crust and an overriding plate are a potential source of tsunamis because ________.',
      choices: [
        'the earthquakes on these faults are underwater and could displace large amounts of water from the oceans',
        'the earthquakes on these faults are underwater and could release large amounts of water from the asthenosphere',
        'one side of the fault is on land and the other is underwater so an earthquake could cause a landslide or tsunami depending on which side moved',
        'oceanic crust is "thrust" over the continent along a megathrust so that forces the land downward producing a tsunami'
      ],
      answer: 0,
      explanation: 'Sudden vertical seafloor motion on a megathrust lifts or drops the whole water column above it, which displaces a large volume of ocean water.',
      concepts: ['megathrust-tsunami'],
      verify: false
    },
    {
      n: 6,
      type: 'tf',
      prompt: 'There are thousands of earthquakes around the world every day.',
      choices: ['True', 'False'],
      answer: 0,
      explanation: 'Thousands of earthquakes happen every day, but most are too small to feel.',
      concepts: ['seismograph'],
      verify: false
    },
    {
      n: 7,
      type: 'tf',
      prompt: 'The Richter earthquake magnitude scale is based on the energy released by the earthquake.',
      choices: ['True', 'False'],
      answer: 1,
      explanation: 'Richter magnitude is based on the amplitude of the largest seismic wave recorded on a seismogram, corrected for distance, and the scale is logarithmic. Moment magnitude is the scale tied to total energy.',
      concepts: ['richter', 'moment-magnitude'],
      verify: false
    },
    {
      n: 8,
      type: 'mc',
      prompt: 'Which of the following is determined by making direct observations in the area affected by an earthquake?',
      choices: [
        'epicenter',
        'focus or hypocenter',
        'magnitude',
        'intensity'
      ],
      answer: 3,
      explanation: 'Intensity comes from observed damage and people\'s reports. The others come from seismograph records.',
      concepts: ['intensity'],
      verify: false
    },
    {
      n: 9,
      type: 'tf',
      prompt: 'Turnagain Heights, was badly damaged by a landslide during the 1964 Alaskan earthquake.',
      choices: ['True', 'False'],
      answer: 0,
      explanation: 'Shaking liquefied a clay layer under the Anchorage neighbourhood of Turnagain Heights, and the bluff slid toward the sea.',
      concepts: ['liquefaction-landslides'],
      verify: false
    },
    {
      n: 10,
      type: 'tf',
      prompt: 'In the open ocean, Tsunamis travel at about the same speed as a commercial airliner.',
      choices: ['True', 'False'],
      answer: 0,
      explanation: 'Tsunamis travel roughly 500-950 km/h in deep water, with low wave heights. They slow and pile up near shore.',
      concepts: ['megathrust-tsunami'],
      verify: false
    }
  );
})();

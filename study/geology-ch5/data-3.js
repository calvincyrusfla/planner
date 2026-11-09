(function () {
  var d = window.STUDY_DATA;
  if (!d || !d.questions) throw new Error('data-3.js: load data.js first');
  d.questions.push(
    {
      n: 11,
      type: "mc",
      prompt: "The figure below shows the intensity of an Earthquake in northern California that affected much of the same areas that were damaged by the 1906 earthquake in a very similar manner. Based on this information, ________. (The original quiz shows an intensity map: VIII near San Francisco and Santa Cruz, fading to II-III toward Nevada.)",
      choices: [
        "people should not be allowed to build homes in the intensity VIII regions because the U.S. taxpayer has to pay to rebuild after such an earthquake",
        "people who live in the high intensity regions should be required to carry earthquake insurance",
        "construction methods are clearly inadequate in the high intensity areas, and all homes in those areas need to be reinforced",
        "in general, the current building codes are adequate, but people should be aware of the hazards in this area"
      ],
      answer: 3,
      explanation: "Damage stayed limited despite strong shaking, which suggests that current building codes are generally adequate even though the hazard in these areas remains real.",
      concepts: ["intensity", "building", "san-andreas-1906"],
      verify: true
    },
    {
      n: 12,
      type: "tf",
      prompt: "A magnitude scale is a measure of the energy released. It does not measure the extent of building damage or loss of life.",
      choices: ["True", "False"],
      answer: 0,
      explanation: "Magnitude describes the energy released, while damage is described by intensity.",
      concepts: ["richter", "moment-magnitude", "intensity"],
      verify: false
    },
    {
      n: 13,
      type: "tf",
      prompt: "Large earthquakes almost always produce new faults because the old faults tend to \"heal\" in the long time that separates most large earthquakes.",
      choices: ["True", "False"],
      answer: 1,
      explanation: "Large earthquakes usually happen again on existing faults, which stay zones of weakness. Elastic rebound repeats on the same fault.",
      concepts: ["elastic-rebound"],
      verify: false
    },
    {
      n: 14,
      type: "mc",
      prompt: "During the earliest history of the earth, ________ .",
      choices: [
        "the earth did not have any radioactive material",
        "the temperature increased so that iron and nickel melted",
        "almost all of the material was radioactive, and it decayed to the compositions we have today.",
        "the earth was cold and lacked any source of heat."
      ],
      answer: 1,
      explanation: "Impacts, compression and radioactive decay heated the early Earth. Molten iron and nickel then sank to form the core, a process called differentiation.",
      concepts: ["early-earth"],
      verify: false
    },
    {
      n: 15,
      type: "mc",
      prompt: "Large megathrust earthquakes in the mountainous regions flanking the Mediterranean sea are generally due to ________.",
      choices: [
        "the collision of the African Plate with Eurasia",
        "the collision of the African Plate with Southeast Asia",
        "the collision of the Indian Plate with Eurasia",
        "the collision of the Indian Plate with Southeast Asia"
      ],
      answer: 0,
      explanation: "The mountainous regions flanking the Mediterranean lie where the African Plate collides with Eurasia, and that collision generally causes their large megathrust earthquakes.",
      concepts: ["plate-boundaries", "megathrust-tsunami"],
      verify: false
    },
    {
      n: 16,
      type: "tf",
      prompt: "The velocity of seismic waves generally increases with depth in the earth.",
      choices: ["True", "False"],
      answer: 0,
      explanation: "Rock gets denser and more rigid with depth, so seismic waves speed up and their paths bend (refract) upward.",
      concepts: ["wave-velocity-depth"],
      verify: false
    },
    {
      n: 17,
      type: "mc",
      prompt: "Why is the Moment magnitude scale favored over the Richter magnitude scale?",
      choices: [
        "The moment magnitude carries the calculated values out to more decimal places so it is more precise.",
        "The Richter magnitude is based on older models of seismographs that are no longer used to record earthquakes.",
        "The Richter magnitude always underestimates the amount of energy released by an earthquake.",
        "The Moment magnitude measures the total energy released by an earthquake."
      ],
      answer: 3,
      explanation: "Moment magnitude measures total energy using fault area x slip x rock rigidity. It does not saturate for very large quakes the way Richter does.",
      concepts: ["moment-magnitude", "richter"],
      verify: false
    }
  );
})();

import { StatusBar } from 'expo-status-bar';
import { View, StyleSheet } from 'react-native';

import { SessionScreen } from './src/screens/SessionScreen';
import { patientSelectionUnavailable } from './src/session/patientSource';

/**
 * Root.
 *
 * The session screen (TASK-025) is wired to `patientSelectionUnavailable`, which
 * is the seam described in `src/session/patientSource.ts`: nothing on this
 * platform can identify a patient or a provider until TASK-025b adds the FHIR
 * patient search route and SMART on FHIR supplies the provider. So this build
 * shows a provider that a visit cannot be started, rather than starting one
 * against an invented patient id — an encounter, a SOAP note and a prior-auth
 * bundle filed against the wrong patient is silent at every layer below this.
 */
export default function App() {
  return (
    <View style={styles.root}>
      <SessionScreen patientSource={patientSelectionUnavailable} />
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
  },
});
